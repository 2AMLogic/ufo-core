import { useEffect, useState } from "react";
import { get, SignedOut, WorkspaceMeta } from "./api";
import { Conversations } from "./Conversations";
import { Fleet } from "./Fleet";
import { Loading } from "./Loading";
import { useParams } from "./nav";
import { Session } from "./Session";

export function App() {
  const [params, navigate] = useParams();
  const [meta, setMeta] = useState<WorkspaceMeta | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [picker, setPicker] = useState(params.ws ?? "");
  const [convoPicker, setConvoPicker] = useState(params.c ?? "");

  const fleetReach = meta?.reach === "fleet";
  const landing = fleetReach && !params.ws && !params.c;

  useEffect(() => setPicker(params.ws ?? ""), [params.ws]);

  useEffect(() => setConvoPicker(params.c ?? ""), [params.c]);

  useEffect(() => {
    document.title = (params.c ?? (landing ? "Fleet" : "Conversations")) + " · Session debugger";
  }, [params.c, landing]);

  useEffect(() => {
    setMeta(null);
    setError(null);
    get<WorkspaceMeta>("workspace")
      .then(setMeta)
      .catch((err: Error) => setError(err));
  }, [params.ws]);

  return (
    <>
      <header>
        <h1 onClick={() => navigate({ ws: null, c: null, t: null })}>Session debugger</h1>
        {fleetReach && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              navigate({ ws: picker.trim() || null, c: null, t: null });
            }}
          >
            <input
              value={picker}
              onChange={(event) => setPicker(event.target.value)}
              placeholder="Workspace domain or UUID"
            />
            <button type="submit">Open</button>
          </form>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            navigate({ c: convoPicker.trim() || null, t: null });
          }}
        >
          <input
            value={convoPicker}
            onChange={(event) => setConvoPicker(event.target.value)}
            placeholder="Conversation UUID"
          />
          <button type="submit">Open</button>
        </form>
        {meta && !landing && (
          <span className="meta crumb" onClick={() => navigate({ c: null, t: null })}>
            Workspace <code>{meta.workspace_id}</code>
            {Object.entries(meta.installations).map(([surface, installation]) => (
              <span key={surface}>
                {` · ${surface} `}
                <code>{installation}</code>
              </span>
            ))}
          </span>
        )}
        <a
          className="cross-link"
          href={`/surface/memory${params.ws ? `?ws=${encodeURIComponent(params.ws)}` : ""}`}
        >
          Memory explorer
        </a>
      </header>
      <main>
        {error instanceof SignedOut ? (
          <div className="empty">
            {error.signIn === null ? (
              <>
                Not signed in. Run <code>ufoctl debugger</code>.
              </>
            ) : (
              <>
                Not signed in. <a href={error.signIn}>Sign in again</a>.
              </>
            )}
          </div>
        ) : error ? (
          <div className="empty">{error.message}</div>
        ) : meta === null ? (
          <Loading />
        ) : landing ? (
          <Fleet navigate={navigate} />
        ) : params.c ? (
          <Session conversationId={params.c} selectedTurn={params.t} navigate={navigate} />
        ) : (
          <Conversations key={params.ws ?? "home"} navigate={navigate} />
        )}
      </main>
    </>
  );
}
