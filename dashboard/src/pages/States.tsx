import { ApiError } from "../api.ts";
import { ErrorState, Pending, Skeleton, Top, TooFew } from "../ui.tsx";
import { EmptyProject } from "./Overview.tsx";

/** Every state a screen can be in, side by side, with sample values. Nothing here calls the API. */
export function States() {
  return (
    <>
      <Top crumb="trimset" title="States board" />
      <div className="body">
        <EmptyProject demo />
        <div className="split">
          <div className="col" style={{ gap: 8 }}>
            <span className="lab">Loading</span>
            <Skeleton />
          </div>
          <div className="col" style={{ gap: 8 }}>
            <span className="lab">Too few labels</span>
              <TooFew title="Guarantee · safe_to_run" n={43} need={600}>
                Unsafe runs ≤ <span className="m">0.5%</span> at 95% confidence needs about <span className="m">600</span> labelled auto-run calls with none unsafe.{" "}
                <span className="m">557</span> more to go.
              </TooFew>
          </div>
        </div>
        <div className="split">
          <div className="col" style={{ gap: 8 }}>
            <span className="lab">Outcomes still pending</span>
            <Pending labelled={0.42} title="58% of last month's refunds have no outcome yet">
              Chargebacks arrive 30 to 90 days after a refund. Metrics use labelled decisions only, and say how many.
            </Pending>
          </div>
          <div className="col" style={{ gap: 8 }}>
            <span className="lab">Server error</span>
            <ErrorState error={new ApiError(503, "Service unavailable", { type: "https://trimset.dev/problems/unavailable" })} retry={() => undefined} />
          </div>
        </div>
      </div>
    </>
  );
}
