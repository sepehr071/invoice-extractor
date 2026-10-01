import { Routes, Route } from "react-router-dom";
import UploadPage from "./pages/UploadPage";
import JobStatusPage from "./pages/JobStatusPage";
import PreviewPage from "./pages/PreviewPage";

export default function App() {
  return (
    <div className="app-shell">
      <main className="app-main">
        <Routes>
          <Route path="/" element={<UploadPage />} />
          <Route path="/jobs/:id/preview" element={<PreviewPage />} />
          <Route path="/jobs/:id" element={<JobStatusPage />} />
        </Routes>
      </main>
    </div>
  );
}
