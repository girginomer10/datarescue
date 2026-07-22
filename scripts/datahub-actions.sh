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
  version)
    uvx --python 3.11 --from "acryl-datahub-actions==${DATAHUB_ACTIONS_VERSION}" \
      datahub-actions --version
    ;;
  *)
    echo "Usage: $0 {run|ready|validate|version}" >&2
    exit 2
    ;;
esac
