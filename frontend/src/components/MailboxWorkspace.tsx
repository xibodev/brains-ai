import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  MailboxAccess,
  MailboxAddress,
  MailMessage,
  MailThread,
  OperatorWorkspace,
} from "../api/types";
import { relativeTime } from "./format";
import { OperatorState, OperatorStatus } from "./OperatorPrimitives";
import { useToast } from "./Toast";
import { classifyAsyncError, type AsyncErrorKind } from "../store/useAsync";

type Folder = "inbox" | "sent";
const FOLDERS: Folder[] = ["inbox", "sent"];
type ComposeMode = { kind: "compose" | "reply" | "forward"; source?: MailMessage };

function operationId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return `browser-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

export function MailboxWorkspace() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { toast } = useToast();
  const [mailboxes, setMailboxes] = useState<MailboxAccess[]>([]);
  const [selectedAddress, setSelectedAddress] = useState("");
  const [folder, setFolder] = useState<Folder>("inbox");
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [thread, setThread] = useState<MailThread | null>(null);
  const [compose, setCompose] = useState<ComposeMode | null>(null);
  const [loadingAccess, setLoadingAccess] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [loadingThread, setLoadingThread] = useState(false);
  const [accessError, setAccessError] = useState<string | null>(null);
  const [messageError, setMessageError] = useState<string | null>(null);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [accessErrorKind, setAccessErrorKind] = useState<AsyncErrorKind>(null);
  const [messageErrorKind, setMessageErrorKind] = useState<AsyncErrorKind>(null);
  const [threadErrorKind, setThreadErrorKind] = useState<AsyncErrorKind>(null);
  const messageRequest = useRef(0);
  const threadRequest = useRef(0);
  const selectedAddressRef = useRef("");

  const selectedMailbox = mailboxes.find((row) => row.address === selectedAddress);
  const canSend = selectedMailbox?.can_send === true;

  const refreshAccess = async () => {
    const next = await api.operatorMailboxAccess();
    setMailboxes(next);
    return next;
  };

  const refreshMessages = async (address = selectedAddress, nextFolder = folder) => {
    const request = ++messageRequest.current;
    if (!address) {
      setMessages([]);
      setLoadingMessages(false);
      return;
    }
    setLoadingMessages(true);
    setMessageError(null);
    setMessageErrorKind(null);
    setMessages([]);
    try {
      const result = nextFolder === "inbox"
        ? await api.operatorMailboxInbox(address, true)
        : await api.operatorMailboxSent(address);
      if (request === messageRequest.current) setMessages(result.messages);
    } catch (error) {
      if (request === messageRequest.current) {
        setMessages([]);
        setMessageError(`${nextFolder} messages unavailable`);
        setMessageErrorKind(classifyAsyncError(error));
      }
    } finally {
      if (request === messageRequest.current) setLoadingMessages(false);
    }
  };

  const loadThread = async (threadId: string, address = selectedAddress) => {
    const request = ++threadRequest.current;
    setLoadingThread(true);
    setThreadError(null);
    setThreadErrorKind(null);
    setThread(null);
    try {
      const next = await api.operatorMailboxThread(threadId, address);
      if (request !== threadRequest.current) return null;
      setThread(next);
      return next;
    } catch (error) {
      if (request !== threadRequest.current) return null;
      setThread(null);
      setThreadError("Thread unavailable");
      setThreadErrorKind(classifyAsyncError(error));
      throw error;
    } finally {
      if (request === threadRequest.current) setLoadingThread(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    setLoadingAccess(true);
    setAccessError(null);
    setAccessErrorKind(null);
    setMailboxes([]);
    api.operatorMailboxAccess()
      .then((nextMailboxes) => {
        if (cancelled) return;
        setMailboxes(nextMailboxes);
        const requested = searchParams.get("mailbox");
        const requestedMailbox = requested
          ? nextMailboxes.find((row) => row.address === requested)
          : undefined;
        if (requested && !requestedMailbox) {
          setSelectedAddress("");
          setAccessError("Mailbox unavailable");
          setAccessErrorKind("not_found");
          return;
        }
        const selected = requestedMailbox
          ?? nextMailboxes.find((row) => row.can_send)
          ?? nextMailboxes[0];
        selectedAddressRef.current = selected?.address ?? "";
        setLoadingMessages(Boolean(selected));
        setSelectedAddress(selected?.address ?? "");
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          selectedAddressRef.current = "";
          setSelectedAddress("");
          setAccessError("Mailbox access unavailable");
          setAccessErrorKind(classifyAsyncError(error));
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingAccess(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    void refreshMessages();
    const requestedThread = searchParams.get("thread");
    if (selectedAddress && requestedThread) {
      void loadThread(requestedThread).catch(() => undefined);
    }
  }, [selectedAddress, folder]);

  const chooseMailbox = (address: string) => {
    messageRequest.current += 1;
    threadRequest.current += 1;
    selectedAddressRef.current = address;
    setMessages([]);
    setLoadingMessages(Boolean(address));
    setMessageError(null);
    setMessageErrorKind(null);
    setThread(null);
    setLoadingThread(false);
    setThreadError(null);
    setThreadErrorKind(null);
    setCompose(null);
    setSelectedAddress(address);
    const next = new URLSearchParams(searchParams);
    next.set("mailbox", address);
    next.delete("thread");
    setSearchParams(next, { replace: true });
  };

  const openThread = async (message: MailMessage) => {
    const address = selectedAddress;
    setCompose(null);
    try {
      const nextThread = await loadThread(message.thread_id, address);
      if (!nextThread || selectedAddressRef.current !== address) return;
      const params = new URLSearchParams(searchParams);
      params.set("mailbox", address);
      params.set("thread", message.thread_id);
      setSearchParams(params, { replace: true });
    } catch {
      // loadThread preserves the non-enumerating API error for the current request.
    }
  };

  const markThreadRead = async () => {
    if (!thread) return;
    const request = ++threadRequest.current;
    const address = selectedAddress;
    setLoadingThread(true);
    setThreadError(null);
    setThreadErrorKind(null);
    try {
      const next = await api.operatorMailboxReadThread(thread.thread_id, address);
      if (request === threadRequest.current) {
        setThread(next);
        await Promise.all([refreshMessages(address), refreshAccess()]);
        if (request === threadRequest.current) toast("Thread marked read");
      }
    } catch (error) {
      if (request === threadRequest.current) {
        setThreadError("Thread update unavailable");
        setThreadErrorKind(classifyAsyncError(error));
      }
    } finally {
      if (request === threadRequest.current) setLoadingThread(false);
    }
  };

  const markInboxRead = async () => {
    const address = selectedAddress;
    const openThreadId = thread?.thread_id;
    const request = ++messageRequest.current;
    setLoadingMessages(true);
    setMessageError(null);
    setMessageErrorKind(null);
    try {
      await api.operatorMailboxReadInbox(address);
      if (request !== messageRequest.current || selectedAddressRef.current !== address) return;
      const [nextMessages, nextMailboxes, nextThread] = await Promise.all([
        api.operatorMailboxInbox(address, true),
        api.operatorMailboxAccess(),
        openThreadId
          ? api.operatorMailboxThread(openThreadId, address)
          : Promise.resolve(null),
      ]);
      if (request !== messageRequest.current || selectedAddressRef.current !== address) return;
      setMessages(nextMessages.messages);
      setMailboxes(nextMailboxes);
      if (nextThread) setThread(nextThread);
      toast("Inbox marked read");
    } catch (error) {
      if (request === messageRequest.current) {
        setMessageError("Inbox update unavailable");
        setMessageErrorKind(classifyAsyncError(error));
      }
    } finally {
      if (request === messageRequest.current) setLoadingMessages(false);
    }
  };

  const conversationRows = messages
    .slice()
    .reverse()
    .filter((message, index, rows) => rows.findIndex((row) => row.thread_id === message.thread_id) === index);
  const hasUnread = (selectedMailbox?.unread_count ?? 0) > 0;
  const chooseFolder = (nextFolder: Folder) => {
    messageRequest.current += 1;
    threadRequest.current += 1;
    setMessages([]);
    setLoadingMessages(Boolean(selectedAddress));
    setMessageError(null);
    setMessageErrorKind(null);
    setThread(null);
    setLoadingThread(false);
    setThreadError(null);
    setThreadErrorKind(null);
    setCompose(null);
    setFolder(nextFolder);
    const params = new URLSearchParams(searchParams);
    params.delete("thread");
    setSearchParams(params, { replace: true });
  };

  return (
    <section className="operator-mailroom" aria-labelledby="mailroom-title">
      <header className="operator-mailroom-head">
        <div>
          <div className="operator-card-kicker">Durable mail</div>
          <h2 id="mailroom-title">Mailbox desk</h2>
          <p>Local acceptance is authoritative. A read, harness nudge, and external copy remain separate evidence.</p>
        </div>
        <div className="operator-action-row">
          <OperatorStatus tone="native">local SQLite</OperatorStatus>
          {!accessError && selectedMailbox && (
            <button
              className="operator-button primary"
              disabled={!canSend}
              title={!canSend ? "Agent mailbox sends require the attached harness and binding proof." : undefined}
              onClick={() => setCompose({ kind: "compose" })}
            >
              Compose mail
            </button>
          )}
        </div>
      </header>
      <OperatorState loading={loadingAccess} error={accessError} kind={accessErrorKind} empty={!loadingAccess && !accessError && mailboxes.length === 0} boundary="mail-access" emptyTitle="No mailbox available" emptyBody="Register an agent mailbox or sign in as a provisioned operator." />
      {!loadingAccess && !accessError && mailboxes.length > 0 && (
        <div className="operator-mail-grid">
          <aside className="operator-mail-rail" aria-label="Mailbox selector">
            <label className="operator-field operator-mail-select">
              <span>Open mailbox</span>
              <select value={selectedAddress} onChange={(event) => chooseMailbox(event.target.value)}>
                {mailboxes.map((mailbox) => (
                  <option key={mailbox.address} value={mailbox.address}>
                    {mailbox.kind === "operator" ? "My operator inbox" : `${mailbox.tool} / ${mailbox.workspace}`}
                  </option>
                ))}
              </select>
            </label>
            <div className="operator-mail-identity">
              <strong>{selectedMailbox?.kind === "operator" ? "Human-bound inbox" : "Agent mailbox"}</strong>
              <code>{selectedAddress}</code>
              <small>
                {canSend ? "Compose, reply, and forward as this operator." : "Read-only browser inspection. Agent sends require adapter proof."}
              </small>
            </div>
            <div className="operator-mail-addresses" aria-label="Openable addresses">
              {mailboxes.map((mailbox) => (
                <button
                  key={mailbox.address}
                  className={mailbox.address === selectedAddress ? "selected" : ""}
                  onClick={() => chooseMailbox(mailbox.address)}
                  aria-current={mailbox.address === selectedAddress ? "true" : undefined}
                >
                  <span>{mailbox.kind === "operator" ? "OP" : (mailbox.tool?.slice(0, 2).toUpperCase() ?? "AG")}</span>
                  <div>
                    <strong>{mailbox.kind === "operator" ? mailbox.owner_operator : mailbox.tool}</strong>
                    <small>{mailbox.workspace ?? "Brains operator"}</small>
                  </div>
                  {mailbox.unread_count > 0 && <b aria-label={`${mailbox.unread_count} unread`}>{mailbox.unread_count}</b>}
                </button>
              ))}
            </div>
          </aside>

          <section className="operator-mail-list" aria-labelledby="mail-list-title">
            <header>
              <div className="operator-mail-tabs" role="tablist" aria-label="Mailbox folders">
                {FOLDERS.map((name, index) => (
                  <button
                    key={name}
                    id={`mail-folder-tab-${name}`}
                    role="tab"
                    aria-selected={folder === name}
                    aria-controls={`mail-folder-panel-${name}`}
                    tabIndex={folder === name ? 0 : -1}
                    className={folder === name ? "active" : ""}
                    onClick={() => chooseFolder(name)}
                    onKeyDown={(event) => {
                      const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
                      const requested = event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? FOLDERS.length - 1
                          : direction
                            ? (index + direction + FOLDERS.length) % FOLDERS.length
                            : -1;
                      if (requested < 0) return;
                      event.preventDefault();
                      chooseFolder(FOLDERS[requested]);
                      event.currentTarget.parentElement
                        ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[requested]
                        ?.focus();
                    }}
                  >
                    {name === "inbox" ? `Inbox${selectedMailbox?.unread_count ? ` ${selectedMailbox.unread_count}` : ""}` : "Sent"}
                  </button>
                ))}
              </div>
              <button
                className="operator-button quiet"
                disabled={folder !== "inbox" || !hasUnread || loadingMessages}
                onClick={() => void markInboxRead()}
              >
                Mark inbox read
              </button>
            </header>
            {FOLDERS.filter((name) => name !== folder).map((name) => (
              <div
                key={name}
                id={`mail-folder-panel-${name}`}
                role="tabpanel"
                aria-labelledby={`mail-folder-tab-${name}`}
                hidden
              />
            ))}
            <div id={`mail-folder-panel-${folder}`} role="tabpanel" aria-labelledby={`mail-folder-tab-${folder}`}>
              <h3 id="mail-list-title" className="operator-visually-hidden">{folder} messages</h3>
              <OperatorState loading={loadingMessages} error={messageError} kind={messageErrorKind} empty={!loadingMessages && !messageError && conversationRows.length === 0} boundary="mail-messages" emptyTitle={`No ${folder} mail`} emptyBody={folder === "inbox" ? "Accepted mail will remain here across agent restarts." : "Messages sent from this mailbox will appear here."} />
              {!loadingMessages && !messageError && conversationRows.length > 0 && (
                <div className="operator-mail-conversations">
                {conversationRows.map((message) => (
                  <button
                    key={message.message_id}
                    className={thread?.thread_id === message.thread_id ? "selected" : ""}
                    onClick={() => void openThread(message)}
                  >
                    <span className={`operator-mail-unread ${message.inbox_delivery?.state === "accepted" ? "active" : ""}`} />
                    <div>
                      <small>{folder === "inbox" ? message.sender : message.deliveries.map((row) => row.recipient).join(", ")}</small>
                      <strong>{message.subject}</strong>
                      <p>{message.body || "No message body"}</p>
                    </div>
                    <time>{relativeTime(message.created_at)}</time>
                  </button>
                ))}
                </div>
              )}
            </div>
          </section>

          <section className="operator-mail-detail" aria-live="polite">
            {compose ? (
              <MailComposer
                mode={compose}
                 sender={selectedAddress}
                 onCancel={() => setCompose(null)}
                 onSent={async (message) => {
                   const address = selectedAddress;
                   if (selectedAddressRef.current !== address) return;
                   setCompose(null);
                   setFolder("sent");
                   await Promise.all([refreshMessages(address, "sent"), refreshAccess()]);
                   const nextThread = await loadThread(message.thread_id, address);
                   if (!nextThread || selectedAddressRef.current !== address) return;
                   const params = new URLSearchParams(searchParams);
                   params.set("mailbox", address);
                  params.set("thread", message.thread_id);
                  setSearchParams(params, { replace: true });
                }}
              />
            ) : (
              <ThreadView
                thread={thread}
                address={selectedAddress}
                canSend={canSend}
                loading={loadingThread}
                error={threadError}
                errorKind={threadErrorKind}
                onMarkRead={() => void markThreadRead()}
                onReply={(source) => setCompose({ kind: "reply", source })}
                onForward={(source) => setCompose({ kind: "forward", source })}
              />
            )}
          </section>
        </div>
      )}
    </section>
  );
}

function ThreadView({
  thread,
  address,
  canSend,
  loading,
  error,
  errorKind,
  onMarkRead,
  onReply,
  onForward,
}: {
  thread: MailThread | null;
  address: string;
  canSend: boolean;
  loading: boolean;
  error: string | null;
  errorKind: AsyncErrorKind;
  onMarkRead: () => void;
  onReply: (message: MailMessage) => void;
  onForward: (message: MailMessage) => void;
}) {
  if (loading || error) return <OperatorState loading={loading} error={error} kind={errorKind} boundary="mail-thread" />;
  if (!thread || thread.messages.length === 0) {
    return <OperatorState loading={false} empty boundary="mail-thread" emptyTitle={thread ? "Empty conversation" : "Select a conversation"} emptyBody={thread ? "No messages are visible in this conversation." : "Opening a thread does not mark it read. Read attribution is always explicit."} />;
  }
  const unread = thread.messages.some((message) =>
    message.sender !== address && message.deliveries.some((delivery) => delivery.recipient === address && delivery.state === "accepted"));
  return (
    <div className="operator-mail-thread">
      <OperatorState loading={false} boundary="mail-thread" />
      <header>
        <div>
          <small>{thread.origin_workspace}</small>
          <h3>{thread.subject}</h3>
          <code>{thread.thread_id}</code>
        </div>
        <button className="operator-button" disabled={!unread} onClick={onMarkRead}>Mark thread read</button>
      </header>
      <div className="operator-mail-timeline">
        {thread.messages.map((message) => (
          <article key={message.message_id} className={message.sender === address ? "outbound" : "inbound"}>
            <header>
              <strong>{message.sender === address ? "You" : message.sender}</strong>
              <time>{relativeTime(message.created_at)}</time>
            </header>
            <h4>{message.subject}</h4>
            {message.body && <p>{message.body}</p>}
            {message.forwarded_message && (
              <blockquote>
                <small>Forwarded from {message.forwarded_message.sender}</small>
                <strong>{message.forwarded_message.subject}</strong>
                <p>{message.forwarded_message.body || "No message body"}</p>
              </blockquote>
            )}
            <footer>
              <div className="operator-delivery-states">
                {message.deliveries.map((delivery) => (
                  <span key={delivery.delivery_id} className={delivery.state}>
                    {delivery.state === "read" ? "Read" : "Accepted"}
                    <small>{delivery.recipient}</small>
                  </span>
                ))}
              </div>
              {canSend && (
                <div className="operator-mail-actions">
                  <button onClick={() => onReply(message)}>Reply</button>
                  <button onClick={() => onForward(message)}>Forward</button>
                </div>
              )}
            </footer>
          </article>
        ))}
      </div>
    </div>
  );
}

function MailComposer({
  mode,
  sender,
  onCancel,
  onSent,
}: {
  mode: ComposeMode;
  sender: string;
  onCancel: () => void;
  onSent: (message: MailMessage) => Promise<void>;
}) {
  const { toast } = useToast();
  const [workspaces, setWorkspaces] = useState<OperatorWorkspace[]>([]);
  const [workspace, setWorkspace] = useState(mode.source?.origin_workspace ?? "");
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(mode.kind !== "reply");
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [workspaceErrorKind, setWorkspaceErrorKind] = useState<AsyncErrorKind>(null);
  const [subject, setSubject] = useState(
    mode.kind === "reply"
      ? `Re: ${mode.source?.subject.replace(/^Re:\s*/i, "") ?? ""}`
      : mode.kind === "forward"
        ? `Fwd: ${mode.source?.subject.replace(/^Fwd:\s*/i, "") ?? ""}`
        : "",
  );
  const [body, setBody] = useState("");
  const [addressBook, setAddressBook] = useState<MailboxAddress[]>([]);
  const [recipients, setRecipients] = useState<string[]>([]);
  const [loadingBook, setLoadingBook] = useState(false);
  const [bookError, setBookError] = useState<string | null>(null);
  const [bookErrorKind, setBookErrorKind] = useState<AsyncErrorKind>(null);
  const [sending, setSending] = useState(false);
  const [idempotencyKey] = useState(operationId);

  useEffect(() => {
    if (mode.kind === "reply") return;
    let cancelled = false;
    setLoadingWorkspaces(true);
    setWorkspaceError(null);
    setWorkspaceErrorKind(null);
    setWorkspaces([]);
    api.operatorWorkspaces()
      .then((rows) => {
        if (!cancelled) {
          setWorkspaces(rows);
          setWorkspace((current) => current || rows[0]?.slug || "");
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setWorkspaceError("Workspace list unavailable");
          setWorkspaceErrorKind(classifyAsyncError(error));
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingWorkspaces(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode.kind]);

  useEffect(() => {
    if (!workspace || mode.kind === "reply") return;
    let cancelled = false;
    setLoadingBook(true);
    setBookError(null);
    setBookErrorKind(null);
    setAddressBook([]);
    api.operatorMailboxPhonebook(workspace)
      .then((rows) => {
        if (!cancelled) {
          setAddressBook(rows.filter((row) => row.address !== sender));
          setRecipients((current) => current.filter((address) => rows.some((row) => row.address === address)));
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setBookError("Address book unavailable");
          setBookErrorKind(classifyAsyncError(error));
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingBook(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspace, mode.kind, sender]);

  const send = async () => {
    if (!workspace || !subject.trim() || !body.trim()) return;
    setSending(true);
    try {
      let result: MailMessage;
      if (mode.kind === "reply" && mode.source) {
        result = await api.operatorMailboxReply(workspace, mode.source.message_id, {
          subject,
          body,
          operation_id: idempotencyKey,
        });
      } else if (mode.kind === "forward" && mode.source) {
        result = await api.operatorMailboxForward(workspace, mode.source.message_id, {
          recipients,
          subject,
          body,
          operation_id: idempotencyKey,
        });
      } else {
        result = await api.operatorMailboxSend(workspace, {
          recipients,
          subject,
          body,
          operation_id: idempotencyKey,
        });
      }
      toast(`${mode.kind === "compose" ? "Mail" : mode.kind === "reply" ? "Reply" : "Forward"} accepted locally`);
      try {
        await onSent(result);
      } catch {
        toast("Mail accepted locally; refresh the mailbox to recover the updated view");
      }
    } catch {
      toast("Mail could not be sent. Retry after checking authorization and local service status.");
    } finally {
      setSending(false);
    }
  };

  const valid = !loadingWorkspaces && !loadingBook && !workspaceError && !bookError
    && workspace && subject.trim() && body.trim()
    && (mode.kind === "reply" || recipients.length > 0);
  return (
    <form className="operator-mail-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
      <header>
        <div>
          <small>{mode.kind === "compose" ? "New durable message" : mode.kind}</small>
          <h3>{mode.kind === "compose" ? "Compose mail" : mode.kind === "reply" ? "Reply in thread" : "Forward with provenance"}</h3>
        </div>
        <button type="button" className="operator-button quiet" onClick={onCancel}>Cancel</button>
      </header>
      <label className="operator-field">
        <span>Origin workspace</span>
        <select
          disabled={mode.kind === "reply" || loadingWorkspaces}
          value={workspace}
          onChange={(event) => {
            setRecipients([]);
            setWorkspace(event.target.value);
          }}
        >
          {mode.kind === "reply" && <option value={workspace}>{workspace}</option>}
          {workspaces.map((row) => <option key={row.slug} value={row.slug}>{row.name || row.slug}</option>)}
        </select>
      </label>
      {mode.kind !== "reply" && <OperatorState loading={loadingWorkspaces} error={workspaceError} kind={workspaceErrorKind} empty={!loadingWorkspaces && !workspaceError && workspaces.length === 0} boundary="mail-compose-workspaces" emptyTitle="No origin Workspace" emptyBody="No visible Workspace is available for this message." />}
      <div className="operator-mail-from"><span>From</span><code>{sender}</code></div>
      {mode.kind === "reply" ? (
        <div className="operator-mail-reply-to">
          <span>Reply to</span>
          <code>
            {mode.source?.sender === sender
              ? mode.source.deliveries.map((delivery) => delivery.recipient).filter(Boolean).join(", ")
              : mode.source?.sender}
          </code>
        </div>
      ) : (
        <fieldset className="operator-address-book">
          <legend>Recipients</legend>
          <OperatorState loading={loadingBook} error={bookError} kind={bookErrorKind} empty={!loadingBook && !bookError && addressBook.length === 0} boundary="mail-address-book" emptyTitle="No authorized recipients" emptyBody="No recipient is visible in this Workspace." />
          {!loadingBook && !bookError && addressBook.length > 0 && addressBook.map((address) => (
            <label key={address.address}>
              <input
                type="checkbox"
                aria-label={address.address}
                checked={recipients.includes(address.address)}
                onChange={(event) => setRecipients((current) => event.target.checked
                  ? [...current, address.address]
                  : current.filter((value) => value !== address.address))}
              />
              <span><strong>{address.kind === "operator" ? address.owner_operator : address.tool}</strong><small>{address.address}</small></span>
            </label>
          ))}
        </fieldset>
      )}
      <label className="operator-field"><span>Subject</span><input autoFocus value={subject} onChange={(event) => setSubject(event.target.value)} maxLength={256} /></label>
      <label className="operator-field"><span>Message</span><textarea value={body} onChange={(event) => setBody(event.target.value)} maxLength={65_536} /></label>
      {mode.kind === "forward" && mode.source && (
        <div className="operator-forward-preview"><small>Original message retained with provenance</small><strong>{mode.source.subject}</strong><p>{mode.source.body || "No message body"}</p></div>
      )}
      <footer>
        <span>Commit first. Notification and SMTP do not affect local acceptance.</span>
        <button className="operator-button primary" type="submit" disabled={!valid || sending}>{sending ? "Committing..." : "Send mail"}</button>
      </footer>
    </form>
  );
}
