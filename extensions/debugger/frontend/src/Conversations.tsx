import { useEffect, useState } from "react";
import { ConversationSummary, get, when } from "./api";
import { Loading } from "./Loading";
import { Params } from "./nav";

const SOURCE_SURFACE = "sources";
const SUBAGENT_SURFACE = "subagent";

export function Conversations(props: { navigate: (next: Partial<Params>) => void }) {
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [showSources, setShowSources] = useState(false);
  const [showSubagents, setShowSubagents] = useState(false);

  useEffect(() => {
    get<ConversationSummary[]>("conversations")
      .then(setConversations)
      .catch(() => setConversations([]));
  }, []);

  if (conversations === null) return <Loading />;
  const shown = conversations.filter(
    (conversation) =>
      (showSources || conversation.surface !== SOURCE_SURFACE) &&
      (showSubagents || conversation.surface !== SUBAGENT_SURFACE),
  );
  const filters = (
    <div className="filters">
      <label>
        <input
          type="checkbox"
          checked={showSources}
          onChange={(event) => setShowSources(event.target.checked)}
        />
        Sources
      </label>
      <label>
        <input
          type="checkbox"
          checked={showSubagents}
          onChange={(event) => setShowSubagents(event.target.checked)}
        />
        Subagents
      </label>
    </div>
  );
  if (shown.length === 0)
    return (
      <>
        {filters}
        <div className="empty">No conversations.</div>
      </>
    );
  return (
    <>
      {filters}
      <table>
        <thead>
          <tr>
            <th>Surface</th>
            <th>Key</th>
            <th>First message</th>
            <th>Member</th>
            <th>Model</th>
            <th>Turns</th>
            <th>Last activity</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {shown.map((conversation) => (
            <tr
              key={conversation.id}
              className="row"
              onClick={() => props.navigate({ c: conversation.id, t: null })}
            >
              <td>
                <span className="chip">{conversation.surface}</span>
              </td>
              <td>
                <code>{conversation.queue_key}</code>
              </td>
              <td className="opening" title={conversation.opening_message ?? ""}>
                {conversation.opening_message ?? "—"}
              </td>
              <td>{conversation.member_email ?? "—"}</td>
              <td>{conversation.model ?? "—"}</td>
              <td>{conversation.turn_count}</td>
              <td>{when(conversation.last_turn_at ?? conversation.created_at)}</td>
              <td>
                {conversation.link && (
                  <a
                    href={conversation.link}
                    target="_blank"
                    rel="noopener"
                    onClick={(event) => event.stopPropagation()}
                  >
                    Open in {conversation.surface}
                  </a>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
