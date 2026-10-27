import { useFetch } from "../hooks";
import { HBars } from "../charts";
import { Loading, PageHead, Pill, Problem, Stat } from "../ui";
import { fmtDate } from "../words";

interface Top { k: number; lift: number; positives_in_top: number; precision: number; recall: number }
interface M { n: number; positives: number; prevalence: number; roc_auc: number; pr_auc: number; brier: number;
  calibration_slope: number; top5: Top; top10: Top; ci95: Record<string, [number, number]> }
interface Ev {
  evaluation: { results: { logistic_regression: M; scorecard: M; diagnostics: Record<string, { met: boolean; target?: string; value: number | number[] }> };
    interventions: { suggested: number; approved: number; treatment: number; control: number; exit_rate_treatment: number;
      exit_rate_control: number; estimated_effect: number; estimated_effect_ci95: [number, number]; true_simulated_effect: number; note: string } } | null;
  model_card: { frozen_at: string; train_dates: string[]; validation_date: string; train_rows: number; train_positives: number;
    validation_rows: number; validation_positives: number } | null;
}

const DIAG: Record<string, string> = {
  lr_lift_at_5pct: "Model finds at least 2× the average rate in its top 5%",
  lr_lift_ci_excludes_1: "That lift is clearly above 1 (95% interval)",
  lr_calibration_slope: "Predicted chances are well calibrated (slope 0.8 to 1.2)",
  scorecard_pr_auc_ratio: "Simple scorecard reaches 75% of the model's precision-recall area",
};
const p1 = (x: number) => `${(x * 100).toFixed(1)}%`;

export default function Evaluation() {
  const { data, error } = useFetch<Ev>("/evaluation");
  if (error) return <Problem error={error} />;
  if (!data) return <Loading what="the evaluation" />;
  if (!data.evaluation || !data.model_card) return <div className="empty"><p>No evaluation report yet.</p><p className="quiet">Run <code>python -m pipeline.run</code> to train and evaluate the model.</p></div>;
  const { results: res, interventions: iv } = data.evaluation;
  const lr = res.logistic_regression, sc = res.scorecard, mc = data.model_card;

  return (
    <>
      <PageHead title="Model evaluation"
        lede={<>Trained on {mc.train_dates.map(fmtDate).join(" and ")}, tuned on {fmtDate(mc.validation_date)}, frozen on {fmtDate(mc.frozen_at)},
          then tested once on the 30 Jun 2026 list it had never seen. Outcome: a voluntary exit within the next two quarters.</>} />

      <section className="stats">
        <Stat value={`${lr.top5.lift.toFixed(1)}×`} label="Leavers in the top 5%, compared with average"
              note={`95% interval ${lr.ci95.lift5[0].toFixed(1)} to ${lr.ci95.lift5[1].toFixed(1)}`} />
        <Stat value={`${lr.top5.positives_in_top} of ${lr.positives}`} label="Test-period leavers found in the top 5%" note={`${p1(lr.top5.recall)} recall`} />
        <Stat value={lr.roc_auc.toFixed(2)} label="Ranking quality (ROC AUC)" note="0.5 is chance, 1.0 is perfect" />
        <Stat value={p1(lr.prevalence)} label="Base rate in the test list" note={`${lr.positives} of ${lr.n.toLocaleString()} people`} />
      </section>

      <div className="two">
        <section className="panel">
          <h2>Model versus simple scorecard</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Measure</th><th className="num">Model</th><th className="num">Scorecard</th></tr></thead>
              <tbody>
                <tr><td>Precision in top 5%</td><td className="num">{p1(lr.top5.precision)}</td><td className="num">{p1(sc.top5.precision)}</td></tr>
                <tr><td>Lift in top 5%</td><td className="num">{lr.top5.lift.toFixed(2)}×</td><td className="num">{sc.top5.lift.toFixed(2)}×</td></tr>
                <tr><td>Recall in top 10%</td><td className="num">{p1(lr.top10.recall)}</td><td className="num">{p1(sc.top10.recall)}</td></tr>
                <tr><td>ROC AUC</td><td className="num">{lr.roc_auc.toFixed(3)}</td><td className="num">{sc.roc_auc.toFixed(3)}</td></tr>
                <tr><td>Precision-recall area</td><td className="num">{lr.pr_auc.toFixed(3)}</td><td className="num">{sc.pr_auc.toFixed(3)}</td></tr>
                <tr><td>Brier score (lower is better)</td><td className="num">{lr.brier.toFixed(4)}</td><td className="num">{sc.brier.toFixed(4)}</td></tr>
                <tr><td>Calibration slope (1.0 is ideal)</td><td className="num">{lr.calibration_slope.toFixed(2)}</td><td className="num">{sc.calibration_slope.toFixed(2)}</td></tr>
              </tbody>
            </table>
          </div>
        </section>
        <section className="panel">
          <h2>Checks agreed before testing</h2>
          <ul className="checks">
            {Object.entries(res.diagnostics).map(([k, d]) => (
              <li key={k}>
                <Pill tone={d.met ? "ok" : "stop"}>{d.met ? "Met" : "Not met"}</Pill>
                <span>{DIAG[k] ?? k}<span className="quiet small"> ({Array.isArray(d.value) ? d.value.map((v) => v.toFixed(2)).join(" to ") : d.value.toFixed(2)})</span></span>
              </li>
            ))}
          </ul>
          <p className="quiet small">A missed check is reported, not tuned away: the test list was used once.</p>
        </section>
      </div>

      <section className="panel">
        <h2>Do conversations help?</h2>
        <p className="quiet">Of {iv.approved} approved cases, {iv.treatment} got a conversation and {iv.control} were kept as a comparison group.
          Share who left voluntarily within two quarters:</p>
        <HBars rows={[{ label: "With a conversation", value: iv.exit_rate_treatment }, { label: "Comparison group", value: iv.exit_rate_control }]}
               fmt={p1} max={Math.max(iv.exit_rate_treatment, iv.exit_rate_control) * 1.2} />
        <p>Estimated difference {iv.estimated_effect >= 0 ? "+" : ""}{(iv.estimated_effect * 100).toFixed(1)} points
          (95% interval {(iv.estimated_effect_ci95[0] * 100).toFixed(1)} to {(iv.estimated_effect_ci95[1] * 100).toFixed(1)}).
          The synthetic data was built with a true effect of {(iv.true_simulated_effect * 100).toFixed(1)} points.</p>
        <p className="quiet small">{iv.note}</p>
      </section>

      <section className="panel">
        <h2>Limits to keep in mind</h2>
        <ul className="plain">
          <li>The data is synthetic. Signals are cleaner than real HR data, so real-world results would likely be weaker.</li>
          <li>Contributions show associations in past data, not causes.</li>
          <li>Leave, pay band, team and manager identity are deliberately not model inputs.</li>
          <li>Trained on {mc.train_rows.toLocaleString()} person-quarters with {mc.train_positives} leavers; validated on {mc.validation_rows.toLocaleString()} with {mc.validation_positives}.</li>
        </ul>
      </section>
    </>
  );
}
