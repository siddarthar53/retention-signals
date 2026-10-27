import { Link } from "react-router-dom";
import { Context } from "../api";
import { useFetch } from "../hooks";
import { Loading, PageHead, Pill, Problem, Purpose } from "../ui";
import { months, STATUS } from "../words";
import { useSession } from "../App";

interface M {
  team_id: string | null; team_name: string | null; scored_in_team: number; purpose: string;
  items: { score_id: string; employee_id: string; themes: string[]; intervention: string | null; context: Context }[];
}

export default function MyCases() {
  const { data, error } = useFetch<M>("/my-cases");
  const { now } = useSession();
  if (error) return <Problem error={error} />;
  if (!data) return <Loading what="your team" />;
  const open = data.items.filter((i) => !i.intervention || ["proposed", "scheduled"].includes(i.intervention));
  return (
    <>
      <PageHead title={data.team_name ?? "My team"}
        lede={data.items.length
          ? `HR suggests a conversation with ${data.items.length} ${data.items.length === 1 ? "person" : "people"} in your team this quarter.`
          : "HR has not suggested any conversations in your team this quarter."} />
      <Purpose text={data.purpose} />
      {data.items.length === 0 ? (
        <div className="empty"><p>Nothing to do here right now.</p><p className="quiet">Regular one-to-ones are still the best way to hear how people are doing.</p></div>
      ) : (
        <>
          <p className="count">{open.length} still to have</p>
          <ul className="cases">
            {data.items.map((i) => (
              <li key={i.score_id}>
                <Link className="case-row mgr" to={`/cases/${i.score_id}`}>
                  <span className="case-who"><strong>{i.employee_id}</strong>
                    <span className="quiet">{i.context.role_title}, joined {months(monthsSince(i.context.hire_date, now))} ago</span></span>
                  <span className="case-why">{i.themes.join(". ")}{i.themes.length ? "." : "General check-in."}</span>
                  <span className="case-state">
                    {i.intervention ? <Pill tone={i.intervention === "completed" ? "ok" : "watch"}>{STATUS[i.intervention]}</Pill>
                      : <Pill tone="signal">Not started</Pill>}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

function monthsSince(iso: string, now: string) {
  return (Date.parse(now) - Date.parse(iso)) / (30.4375 * 864e5);
}
