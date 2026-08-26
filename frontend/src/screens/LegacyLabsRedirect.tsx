import { Navigate, useParams } from "react-router-dom";

export function LegacyLabsRedirect({
  to,
  parameter,
}: {
  to: string;
  parameter: "id" | "slug" | "code" | "section";
}) {
  const { id, slug, code, section } = useParams();
  const value = { id, slug, code, section }[parameter];
  return <Navigate to={value ? `${to}/${encodeURIComponent(value)}` : to} replace />;
}
