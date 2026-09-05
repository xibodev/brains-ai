import { useCoreNavigation } from "../coreRoutes";
import { OperatorPageHead, OperatorState } from "../components/OperatorPrimitives";

export function NotFound({ resource = "Page" }: { resource?: string }) {
  const navigation = useCoreNavigation();
  return (
    <div className="operator-page" data-testid="not-found" data-route-state="not-found">
      <OperatorPageHead
        eyebrow="Unknown destination"
        title={`${resource} not found`}
        lede="This address does not identify an available Brains view. No private route or resource details were disclosed."
      />
      <OperatorState loading={false} kind="not_found" error={`${resource} is unavailable or does not exist.`} />
      <div className="operator-action-row">
        <button className="operator-button" onClick={() => navigation.back()}>Go back</button>
        <button className="operator-button primary" onClick={() => navigation.open("/command-center")}>Open Command Center</button>
      </div>
    </div>
  );
}
