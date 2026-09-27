import { useEffect, useState } from "react";
import { RecoveryRecord, RolloverRecord, get } from "./api";
import { Loading } from "./Loading";

export function Rollovers(props: { conversationId: string }) {
  const [indices, setIndices] = useState<number[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [record, setRecord] = useState<RolloverRecord | null>(null);

  useEffect(() => {
    setIndices(null);
    setOpen(null);
    setRecord(null);
    get<number[]>(`conversations/${props.conversationId}/rollovers`)
      .then(setIndices)
      .catch(() => setIndices([]));
  }, [props.conversationId]);

  useEffect(() => {
    if (open === null) return;
    setRecord(null);
    get<RolloverRecord>(`conversations/${props.conversationId}/rollovers/${open}`).then(
      setRecord,
    );
  }, [props.conversationId, open]);

  if (indices === null) return <Loading />;
  if (indices.length === 0) return <div className="empty">No rollovers.</div>;
  return (
    <section className="panel">
      <h2>Rollovers</h2>
      <div>
        {indices.map((index) => (
          <span
            key={index}
            className="chip rollover"
            onClick={() => setOpen(open === index ? null : index)}
          >
            Rollover #{index}
          </span>
        ))}
      </div>
      {open !== null && record && (
        <>
          <dl className="kv">
            <dt>Messages</dt>
            <dd>
              {record.before.length} before, {record.after.length} after
            </dd>
            <Recovery recovery={record.recovery} />
          </dl>
          <details>
            <summary>Before window (verbatim)</summary>
            <pre>{JSON.stringify(record.before, null, 2)}</pre>
          </details>
          <details>
            <summary>After window (what replaced it)</summary>
            <pre>{JSON.stringify(record.after, null, 2)}</pre>
          </details>
        </>
      )}
    </section>
  );
}

function Recovery(props: { recovery: RecoveryRecord }) {
  const { recovery } = props;
  return (
    <>
      <dt>History</dt>
      <dd>
        <code>{recovery.history_path}</code> lines {recovery.first_entry_id}–
        {recovery.last_entry_id}
        {recovery.history_lost ? " (earlier history lost with the sandbox)" : ""}
      </dd>
      {recovery.objective && (
        <>
          <dt>Objective (spawned turn)</dt>
          <dd>{recovery.objective}</dd>
        </>
      )}
      {recovery.handoff && (
        <>
          <dt>Handoff</dt>
          <dd>{recovery.handoff}</dd>
        </>
      )}
      {recovery.checkpoint && (
        <>
          <dt>Checkpoint (possibly stale)</dt>
          <dd>{recovery.checkpoint}</dd>
        </>
      )}
      {recovery.user_inputs.length > 0 && (
        <>
          <dt>Member messages</dt>
          <dd>
            {recovery.user_inputs.map((text, at) => (
              <div key={at}>{text}</div>
            ))}
          </dd>
        </>
      )}
      {recovery.pending_results.length > 0 && (
        <>
          <dt>Unread results</dt>
          <dd>
            {recovery.pending_results.map((result) => (
              <div key={result.entry_id + result.call}>
                <code>{result.call}</code> — entry {result.entry_id}
                {result.truncated ? " (trimmed)" : ""}
              </div>
            ))}
          </dd>
        </>
      )}
      {recovery.checklist.length > 0 && (
        <>
          <dt>Checklist</dt>
          <dd>{recovery.checklist.join(" · ")}</dd>
        </>
      )}
      {recovery.loaded_skills.length > 0 && (
        <>
          <dt>Skills dropped</dt>
          <dd>{recovery.loaded_skills.join(" · ")}</dd>
        </>
      )}
    </>
  );
}
