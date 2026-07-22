import { Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { CasePage } from "./pages/CasePage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PolicyPage } from "./pages/PolicyPage";
import { QueuePage } from "./pages/QueuePage";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<QueuePage />} />
        <Route path="cases/:caseId" element={<CasePage />} />
        <Route path="policy" element={<PolicyPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
