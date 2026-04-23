import { useParams } from "react-router-dom";
import { LegalDocumentViewer } from "../components/legal/LegalDocumentViewer";

export default function LegalDocument() {
  const { slug } = useParams();
  return <LegalDocumentViewer slug={slug} />;
}
