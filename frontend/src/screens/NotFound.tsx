import { useNavigate } from "react-router-dom";
import { OperatorPageHead, OperatorState } from "../components/OperatorPrimitives";

export function NotFound({ resource = "Page" }: { resource?: string }) {
  const navigate = useNavigate();
  return (
    <div className="operator-page" data-testid="not-found" data-route-state="not-found">
      <OperatorPageHead
        eyebrow="Unknown destination"
        title={`${resource} not found`}
        lede="This address does not identify an available Brains view. No private route or resource details were disclosed."
      />
      <OperatorState loading={false} kind="not_found" error={`${resource} is unavailable or does not exist.`} />
      <div className="operator-action-row">
        <button className="operator-button" onClick={() => navigate(-1)}>Go back</button>
        <button className="operator-button primary" onClick={() => navigate("/command-center")}>Open Command Center</button>
      </div>
    </div>
  );
}
