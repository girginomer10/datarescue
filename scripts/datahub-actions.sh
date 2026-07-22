#!/usr/bin/env bash
set -euo pipefail

readonly DATAHUB_ACTIONS_VERSION="${DATAHUB_ACTIONS_VERSION:-1.6.0.15}"
readonly ACTIONS_RECIPE="${DATAHUB_ACTIONS_RECIPE:-demo/datahub/schema-drift-actions.yml}"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "${REPO_ROOT}"

run_actions() {
  exec uv run \
    --isolated \
    --python 3.11 \
    --with "acryl-datahub-actions==${DATAHUB_ACTIONS_VERSION}" \
    --with-editable . \
    -- datahub-actions --debug actions run -c "${ACTIONS_RECIPE}"
}

validate_actions() {
  uv run \
    --isolated \
    --python 3.11 \
    --with "acryl-datahub-actions==${DATAHUB_ACTIONS_VERSION}" \
    --with-editable . \
    -- python - "${ACTIONS_RECIPE}" <<'PY'
import sys
from pathlib import Path

from datahub.configuration.config_loader import load_config_file
from datahub_actions.action.action_registry import action_registry
from datahub_actions.filter.filter_registry import filter_registry
from datahub_actions.pipeline.pipeline_config import PipelineConfig
from datahub_actions.plugin.filter.event_type_filter import EventTypeFilterConfig
from datahub_actions.plugin.source.kafka.kafka_event_source import KafkaEventSourceConfig
from datahub_actions.source.event_source_registry import event_source_registry

recipe = Path(sys.argv[1])
raw = load_config_file(recipe)
config = PipelineConfig.model_validate(raw)
event_source_registry.get(config.source.type)
KafkaEventSourceConfig.model_validate(config.source.config or {})
action_class = action_registry.get(config.action.type)
action_class.create(config.action.config or {}, None).close()
for filter_spec in config.filters or []:
    filter_registry.get(filter_spec.type)
    if filter_spec.type == "event_type":
        EventTypeFilterConfig.model_validate(filter_spec.config or {})
print(
    f"Validated {recipe} with acryl-datahub-actions: "
    f"source={config.source.type}, action={config.action.type}"
)
PY
}

wait_until_ready() {
  uv run \
    --isolated \
    --python 3.11 \
    --with "acryl-datahub-actions==${DATAHUB_ACTIONS_VERSION}" \
    -- python - <<'PY'
import os
import time

from confluent_kafka.admin import AdminClient

bootstrap = os.environ["DATAHUB_KAFKA_BOOTSTRAP"]
group = os.environ.get("DATAHUB_ACTIONS_GROUP", "datarescue-schema-drift")
topic = os.environ.get("DATAHUB_MCL_TOPIC", "MetadataChangeLog_Versioned_v1")
admin = AdminClient({"bootstrap.servers": bootstrap})
deadline = time.monotonic() + 60
last_error = f"consumer group has no assignment for {topic}"
while time.monotonic() < deadline:
    try:
        future = admin.describe_consumer_groups([group], request_timeout=5)[group]
        description = future.result(timeout=6)
        assignments = [
            partition
            for member in description.members
            for partition in member.assignment.topic_partitions
        ]
        if any(partition.topic == topic for partition in assignments):
            print(
                f"DataHub Actions consumer group {group!r} is assigned to {topic} "
                f"({len(assignments)} partition(s))."
            )
            raise SystemExit(0)
        last_error = (
            f"consumer group state={description.state}, "
            f"assigned_partitions={len(assignments)}"
        )
    except Exception as error:  # Kafka errors are retried until the bounded deadline.
        last_error = str(error)
    time.sleep(1)
raise SystemExit(f"DataHub Actions consumer group was not ready: {last_error}")
PY
}

capture_topic_end_offsets() {
  local output_path="${1:?offset snapshot path is required}"
  DATAHUB_ACTIONS_OFFSETS_FILE="${output_path}" uv run \
    --isolated \
    --python 3.11 \
    --with "acryl-datahub-actions==${DATAHUB_ACTIONS_VERSION}" \
    -- python - <<'PY'
import json
import os
from pathlib import Path

from confluent_kafka import TopicPartition
from confluent_kafka.admin import AdminClient, OffsetSpec

bootstrap = os.environ["DATAHUB_KAFKA_BOOTSTRAP"]
topic = os.environ.get("DATAHUB_MCL_TOPIC", "MetadataChangeLog_Versioned_v1")
output = Path(os.environ["DATAHUB_ACTIONS_OFFSETS_FILE"])
admin = AdminClient({"bootstrap.servers": bootstrap})
metadata = admin.list_topics(topic=topic, timeout=10)
topic_metadata = metadata.topics.get(topic)
if topic_metadata is None or topic_metadata.error is not None:
    raise SystemExit(f"Cannot inspect {topic}: {getattr(topic_metadata, 'error', None)}")
requests = {
    TopicPartition(topic, partition): OffsetSpec.latest()
    for partition in topic_metadata.partitions
}
offsets = {
    f"{partition.topic}:{partition.partition}": future.result(timeout=10).offset
    for partition, future in admin.list_offsets(requests, request_timeout=10).items()
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"topic": topic, "offsets": offsets}, sort_keys=True) + "\n")
print(f"Captured pre-drift end offsets for {topic}: {offsets}")
PY
}

restore_group_offsets() {
  local input_path="${1:?offset snapshot path is required}"
  local compose_file="${DATAHUB_QUICKSTART_COMPOSE:-${HOME}/.datahub/quickstart/docker-compose.yml}"
  local group="${DATAHUB_ACTIONS_GROUP:-datarescue-schema-drift}"
  local topic="${DATAHUB_MCL_TOPIC:-MetadataChangeLog_Versioned_v1}"
  local partition=""
  local offset=""
  local restored=0
  local expected=0
  [[ -f "${compose_file}" ]] || {
    echo "DataHub Quickstart compose file is missing: ${compose_file}" >&2
    exit 1
  }
  while IFS=$'\t' read -r partition offset; do
    expected=$((expected + 1))
    for _attempt in $(seq 1 15); do
      if docker compose -f "${compose_file}" -p datahub exec -T kafka-broker \
        kafka-consumer-groups --bootstrap-server broker:29092 \
        --group "${group}" --topic "${topic}:${partition}" \
        --reset-offsets --to-offset "${offset}" --execute </dev/null >/dev/null; then
        restored=$((restored + 1))
        break
      fi
      sleep 1
    done
  done < <(
    DATAHUB_ACTIONS_OFFSETS_FILE="${input_path}" \
      DATAHUB_MCL_TOPIC="${topic}" uv run python - <<'PY'
import json
import os
from pathlib import Path

snapshot = json.loads(Path(os.environ["DATAHUB_ACTIONS_OFFSETS_FILE"]).read_text())
topic = os.environ["DATAHUB_MCL_TOPIC"]
if snapshot.get("topic") != topic or not isinstance(snapshot.get("offsets"), dict):
    raise SystemExit("Offset snapshot does not match the configured MCL topic")
for key, offset in snapshot["offsets"].items():
    recorded_topic, raw_partition = key.rsplit(":", 1)
    if recorded_topic != topic:
        raise SystemExit(f"Offset snapshot contains an unexpected topic: {recorded_topic}")
    print(f"{int(raw_partition)}\t{int(offset)}")
PY
  )
  [[ "${expected}" -gt 0 && "${restored}" -eq "${expected}" ]] || {
    echo "Only ${restored}/${expected} consumer-group offsets were restored." >&2
    exit 1
  }
  printf "Restored consumer group '%s' to %s recorded pre-drift offset(s) for %s.\n" \
    "${group}" "${restored}" "${topic}"
}

wait_until_caught_up() {
  uv run \
    --isolated \
    --python 3.11 \
    --with "acryl-datahub-actions==${DATAHUB_ACTIONS_VERSION}" \
    -- python - <<'PY'
import os
import time

from confluent_kafka import ConsumerGroupTopicPartitions, TopicPartition
from confluent_kafka.admin import AdminClient, OffsetSpec

bootstrap = os.environ["DATAHUB_KAFKA_BOOTSTRAP"]
group = os.environ.get("DATAHUB_ACTIONS_GROUP", "datarescue-schema-drift")
topic = os.environ.get("DATAHUB_MCL_TOPIC", "MetadataChangeLog_Versioned_v1")
admin = AdminClient({"bootstrap.servers": bootstrap})
deadline = time.monotonic() + 90
last = "no committed offsets"
while time.monotonic() < deadline:
    try:
        request = ConsumerGroupTopicPartitions(group)
        committed = admin.list_consumer_group_offsets(
            [request], request_timeout=5
        )[group].result(timeout=6).topic_partitions
        topic_offsets = [item for item in committed if item.topic == topic and item.offset >= 0]
        if topic_offsets:
            latest_requests = {
                TopicPartition(item.topic, item.partition): OffsetSpec.latest()
                for item in topic_offsets
            }
            latest = {
                (item.topic, item.partition): future.result(timeout=6).offset
                for item, future in admin.list_offsets(
                    latest_requests, request_timeout=5
                ).items()
            }
            lag = sum(
                max(0, latest[(item.topic, item.partition)] - item.offset)
                for item in topic_offsets
            )
            if lag == 0:
                print(f"DataHub Actions consumer group {group!r} caught up on {topic}.")
                raise SystemExit(0)
            last = f"remaining lag={lag}"
    except Exception as error:
        last = str(error)
    time.sleep(1)
raise SystemExit(f"DataHub Actions consumer group did not catch up: {last}")
PY
}

case "${1:-}" in
  run)
    run_actions
    ;;
  validate)
    validate_actions
    ;;
  ready)
    wait_until_ready
    ;;
  capture-end)
    capture_topic_end_offsets "${2:-}"
    ;;
  restore)
    restore_group_offsets "${2:-}"
    ;;
  caught-up)
    wait_until_caught_up
    ;;
  version)
    uvx --python 3.11 --from "acryl-datahub-actions==${DATAHUB_ACTIONS_VERSION}" \
      datahub-actions --version
    ;;
  *)
    echo "Usage: $0 {run|ready|capture-end PATH|restore PATH|caught-up|validate|version}" >&2
    exit 2
    ;;
esac
