import { ArrowLeft } from "lucide-react";
import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <main className="page not-found" id="main-content">
      <p className="eyebrow">404 · ROUTE NOT FOUND</p>
      <h1>No evidence at this address.</h1>
      <p>Return to the rescue queue to inspect active schema-drift cases.</p>
      <Link className="primary-outline-button" to="/"><ArrowLeft aria-hidden="true" size={15} /> Rescue queue</Link>
    </main>
  );
}
