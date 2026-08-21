import { useEffect, useRef, useState } from "react";
import {
  Check, ChevronLeft, ChevronRight, Clock3, MessageCircle, MoreHorizontal,
  Paperclip, Plus, RefreshCw, SendHorizontal, Sparkles, UserRound, X,
} from "lucide-react";
import "./App.css";
import Logo from "./Logo.jsx";
import ScheduleReviewIcon from "./assets/list-ul-square.svg";
import { apiFetch, supabase } from "./supabase.js";

// Browser-supported timezones used in the profile selector.
const ZONES = Intl.supportedValuesOf("timeZone");
// Small helpers that keep date selection and timeline times consistent.
const addDays = (date, days) => new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
const dateKey = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const formatTime = (value, timezone) => new Intl.DateTimeFormat("en-US", {
  hour: "numeric", minute: "2-digit", timeZone: timezone,
}).format(new Date(value));
const formatDay = (date) => date.toLocaleDateString("en-US", { weekday: "short" });
const DAY_OPTIONS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const TIME_OPTIONS = Array.from({ length: 48 }, (_, index) => `${String(Math.floor(index / 2)).padStart(2, "0")}:${index % 2 ? "30" : "00"}`);

async function responseError(response, fallback) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const body = await response.json().catch(() => ({}));
    return typeof body.detail === "string" ? body.detail : fallback;
  }
  return (await response.text().catch(() => "")).trim() || fallback;
}

export default function App() {
  // Restore profile choices after a refresh so the UI stays personalized.
  const savedProfile = JSON.parse(localStorage.getItem("cal-profile") || "{}");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([]);
  const [thinking, setThinking] = useState(false);
  const [name, setName] = useState(savedProfile.name || "Jing");
  const [timezone, setTimezone] = useState(savedProfile.timezone || "America/Toronto");
  const [navOpen, setNavOpen] = useState(false);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [modal, setModal] = useState(null);
  const [role, setRole] = useState("");
  const [activeEvent, setActiveEvent] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [selectedDate, setSelectedDate] = useState(new Date());
  const [weekStart, setWeekStart] = useState(new Date());
  const [calendar, setCalendar] = useState({ events: [], suggestions: [] });
  const [calendarError, setCalendarError] = useState("");
  const [calendarLoading, setCalendarLoading] = useState(false);
  const [pendingSchedule, setPendingSchedule] = useState(null);
  const [importStatus, setImportStatus] = useState("");
  const [googleConnected, setGoogleConnected] = useState(false);
  const textRef = useRef(null);
  const scrollRef = useRef(null);
  const fileRef = useRef(null);
  const latestBotMessageRef = useRef(null);

  // Load saved chats for the Recent chats sidebar.
  const loadConversations = async () => {
    try {
      const response = await apiFetch("/api/conversations");
      if (response.ok) setConversations((await response.json()).conversations);
    } catch { /* Backend availability is shown when the user sends a message. */ }
  };

  // Request the selected day's real Google Calendar events from the backend.
  const loadCalendar = async () => {
    try {
      setCalendarError(""); setCalendarLoading(true);
      const response = await apiFetch(`/api/calendar?date=${dateKey(selectedDate)}&timezone=${encodeURIComponent(timezone)}`);
      if (!response.ok) throw new Error(await responseError(response, "Unable to load calendar"));
      setCalendar(await response.json());
    } catch (error) {
      setCalendar({ events: [], suggestions: [] });
      const missingScope = /insufficient authentication scopes|insufficient permission/i.test(error.message || "");
      if (missingScope) {
        await apiFetch("/api/auth/google/token", { method: "DELETE" }).catch(() => null);
        setGoogleConnected(false);
        setCalendarError("Reconnect Google Calendar to grant calendar access.");
      } else {
        setCalendarError(error.message || "Unable to load calendar");
      }
    } finally { setCalendarLoading(false); }
  };

  // Fetch chats once, refresh calendar data when the day or timezone changes,
  // and keep UI-only profile preferences in the browser.
  useEffect(() => { loadConversations(); }, []);
  useEffect(() => { apiFetch("/api/profile").then(r => r.ok ? r.json() : {}).then(p => setRole(p.role || "")).catch(() => {}); }, []);
  useEffect(() => { loadCalendar(); }, [selectedDate, timezone]);
  useEffect(() => { localStorage.setItem("cal-profile", JSON.stringify({ name, timezone })); }, [name, timezone]);
  useEffect(() => {
    let active = true;
    async function finishGoogleConnection() {
      const { data: { session } } = await supabase.auth.getSession();
      const calendarConnectionPending = localStorage.getItem("cal-calendar-oauth-pending") === "true";
      if (calendarConnectionPending && session?.provider_token) {
        await apiFetch("/api/auth/google/token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            access_token: session.provider_token,
            refresh_token: session.provider_refresh_token || null,
            expires_in: 3600,
          }),
        }).catch(() => null);
        localStorage.removeItem("cal-calendar-oauth-pending");
      }
      const response = await apiFetch("/api/auth/google/status").catch(() => null);
      if (active && response?.ok) setGoogleConnected((await response.json()).connected);
    }
    finishGoogleConnection();
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (thinking && scrollRef.current) {
      // Keep the typing indicator visible while Cal is preparing a response.
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    } else if (messages.at(-1)?.role === "bot") {
      // A finished reply should begin at the top of the visible chat area.
      latestBotMessageRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [messages, thinking]);
  useEffect(() => {
    const el = textRef.current;
    if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 140)}px`; }
  }, [input]);

  // Fill the composer from an Ask Cal suggestion and move focus to it.
  const prompt = (text) => {
    setInput(text);
    setTimeout(() => textRef.current?.focus(), 0);
  };
  // A new chat starts without deleting older conversations from memory.
  const startNewChat = () => {
    setMessages([]); setConversationId(null); setPendingSchedule(null); setImportStatus(""); setInput("");
    setTimeout(() => textRef.current?.focus(), 0);
  };
  // Restore all messages from a chat selected in the sidebar.
  const openConversation = async (id) => {
    try {
      const response = await apiFetch(`/api/conversations/${id}`);
      if (!response.ok) throw new Error();
      const conversation = await response.json();
      setConversationId(conversation.id); setMessages(conversation.messages); setPendingSchedule(conversation.pending_schedule || null); setImportStatus(""); setAccountMenuOpen(false);
    } catch { setCalendarError("I couldn't load that conversation."); }
  };

  async function send() {
    // Send a text message with the active chat ID, profile, and timezone.
    const text = input.trim();
    if (!text || thinking) return;
    setMessages((old) => [...old, { role: "user", text }]);
    setInput(""); setThinking(true);
    try {
      const response = await apiFetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: conversationId, timezone, name }),
      });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Request failed: ${response.status}`);
      const reply = await response.json();
      setConversationId(reply.conversation_id);
      setMessages((old) => [...old, { role: "bot", text: reply.response }]);
      loadConversations(); loadCalendar();
    } catch (error) {
      setMessages((old) => [...old, { role: "bot", error: true, text: error instanceof TypeError
        ? "I couldn't reach the calendar service. Make sure the backend is running on port 8000."
        : error.message || "I couldn't reach the calendar service. Please try again." }]);
    } finally { setThinking(false); }
  }

  async function uploadSchedule(file) {
    // Send PDFs and images as FormData; the backend extracts classes before importing.
    if (!file || thinking) return;
    const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;
    setMessages((old) => [...old, { role: "user", text: file.name, image_url: previewUrl }]);
    setThinking(true);
    try {
      const form = new FormData();
      form.append("file", file);
      if (conversationId) form.append("conversation_id", conversationId);
      form.append("timezone", timezone);
      form.append("name", name);
      const response = await apiFetch("/api/schedule/upload", { method: "POST", body: form });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "Schedule upload failed");
      const reply = await response.json();
      // The response includes the new/active chat ID needed for follow-up questions.
      setConversationId(reply.conversation_id);
      setMessages((old) => [...old, { role: "bot", text: reply.response }]);
      setPendingSchedule(reply.classes ? { classes: reply.classes } : null);
      setImportStatus("");
      loadConversations();
    } catch (error) {
      setMessages((old) => [...old, { role: "bot", error: true, text: error.message || "I couldn't analyze that schedule." }]);
    } finally { setThinking(false); if (fileRef.current) fileRef.current.value = ""; }
  }

  const currentDate = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: timezone }).format(new Date()).toUpperCase();
  const currentHour = Number(new Intl.DateTimeFormat("en-US", { hour: "numeric", hourCycle: "h23", timeZone: timezone }).format(new Date()));
  const greeting = currentHour < 12 ? "Good morning" : currentHour < 17 ? "Good afternoon" : "Good evening";
  const week = Array.from({ length: 7 }, (_, index) => addDays(weekStart, index));

  const connectGoogleCalendar = async () => {
    localStorage.setItem("cal-calendar-oauth-pending", "true");
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: window.location.origin,
        scopes: "https://www.googleapis.com/auth/calendar",
        queryParams: { access_type: "offline", prompt: "consent", include_granted_scopes: "true" },
      },
    });
  };

  const saveProfile = async () => { await apiFetch("/api/profile", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) }); };
  return <main className={`app-shell ${navOpen ? "nav-open" : ""}`}>
    <aside className="sidebar">
      <button className="brand" onClick={() => setNavOpen((open) => !open)}><Logo size={46} active /><span className="brandName">Cal</span></button>
      <nav><button className="nav-item active"><MessageCircle size={19} /><span>Workspace</span></button></nav>
      <div className="thread-list">
        <p>Recent chats</p>
        {conversations.length ? conversations.map((thread) => <button key={thread.id} onClick={() => openConversation(thread.id)}>{thread.title}</button>) : <small className="empty-threads"><span>Start a conversation</span>Your recent chats will appear here.</small>}
      </div>
      <button className="account-button" onClick={() => setAccountMenuOpen((open) => !open)}>
        <b className="avatar">{name[0]?.toUpperCase()}</b><span><strong>{name}</strong><small>{timezone.replace("_", " ")}</small></span><MoreHorizontal size={18} />
      </button>
    </aside>
    <section className="content">
      <header className="topbar"><div><p className="eyebrow">{currentDate}</p><h1>{greeting}, {name}</h1></div><div>
        {!googleConnected && <button className="connect-calendar" onClick={connectGoogleCalendar}>Connect Google Calendar</button>}
        <button className="new-plan-header" onClick={startNewChat} aria-label="New chat"><Plus size={19} /></button>
      </div></header>
      <Chat {...{ messages, thinking, input, setInput, textRef, scrollRef, fileRef, latestBotMessageRef, send, uploadSchedule, prompt, selectedDate, setSelectedDate, week, weekStart, setWeekStart, calendar, calendarError, calendarLoading, timezone, loadCalendar, conversationId, pendingSchedule, setPendingSchedule, importStatus, setImportStatus, setModal, setActiveEvent }} />
    </section>
    {accountMenuOpen && <div className="account-menu"><button className="close" onClick={() => setAccountMenuOpen(false)}><X size={16} /></button>
      <button onClick={() => { setModal("profile"); setAccountMenuOpen(false); }}><UserRound size={17} />Profile</button>
      <button onClick={() => supabase.auth.signOut()}><X size={17} />Sign out</button>
    </div>}
    {modal && <div className="modal-backdrop" onClick={() => setModal(null)}><section className={`account-modal ${modal === "schedule-review" ? "schedule-modal" : ""}`} onClick={(event) => event.stopPropagation()}><button className="close" onClick={() => setModal(null)}><X size={18} /></button>
      {modal === "profile" ? <Profile {...{ name, setName, timezone, setTimezone, role, setRole, saveProfile }} onClose={() => setModal(null)} /> : modal === "event" ? <EventEditor event={activeEvent} timezone={timezone} refresh={loadCalendar} close={() => setModal(null)} /> : <ScheduleReview {...{ conversationId, pendingSchedule, setPendingSchedule, timezone, loadCalendar, setImportStatus, importStatus }} onClose={() => setModal(null)} />}
    </section></div>}
  </main>;
}

function Composer({ input, setInput, textRef, fileRef, send, uploadSchedule, thinking }) {
  // The hidden file input is opened by the paperclip button.
  return <div className="composer-wrap"><div className="composer"><input className="schedule-input" ref={fileRef} type="file" accept="application/pdf,image/png,image/jpeg,image/webp" onChange={(event) => uploadSchedule(event.target.files?.[0])} /><button className="attach-button" onClick={() => fileRef.current?.click()} disabled={thinking} aria-label="Upload a class schedule"><Paperclip size={18} /></button><textarea ref={textRef} value={input} placeholder="Ask Cal to organize something…" rows="1" onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} /><button className="send-button" onClick={send} disabled={!input.trim() || thinking}><SendHorizontal size={19} /></button></div><small className="upload-hint">Upload a class-schedule image or PDF</small></div>;
}

function Chat(props) {
  // Keep the calendar landing view until the first chat message is sent.
  const { messages, thinking, input, setInput, textRef, scrollRef, send } = props;
  if (!messages.length) return <section className="chat chat-landing"><Today {...props} compact /><Composer {...props} /></section>;
  const activity = input.toLowerCase().includes("schedule") || input.toLowerCase().includes("calendar") ? "Checking your calendar…" : "Cal is thinking…"; return <section className="chat conversation"><div className="messages" ref={scrollRef}>{messages.map((message, index) => <div className={`row ${message.role}`} key={`${message.created_at || "message"}-${index}`} ref={message.role === "bot" && index === messages.length - 1 ? props.latestBotMessageRef : null}><div className={`message ${message.error ? "error" : ""}`}>{message.role === "bot" ? <FormattedMessage text={message.text} /> : message.image_url ? <figure className="uploaded-image"><img src={message.image_url} alt={message.text || "Uploaded schedule"} /><figcaption>{message.text}</figcaption></figure> : message.text}</div></div>)}{props.pendingSchedule && <div className="row bot"><button className="schedule-review-button" onClick={() => props.setModal("schedule-review")}><img src={ScheduleReviewIcon} alt="" /><span>Review schedule</span><ChevronRight size={17} /></button></div>}{thinking && <div className="row bot"><div className="thinking-status"><div className="typing"><i /><i /><i /></div>{activity}</div></div>}</div><Composer {...props} /></section>;
}

function FormattedMessage({ text }) {
  // Render simple lists and bold emphasis from Cal without injecting HTML.
  const bold = (value) => value.split(/(\*\*[^*]+\*\*)/g).map((part, index) => part.startsWith("**") ? <strong key={index}>{part.slice(2, -2)}</strong> : part);
  return <>{text.split("\n").map((line, index) => /^\s*(?:[-•]|\d+\.)\s+/.test(line) ? <div className="message-list" key={index}>{bold(line.replace(/^\s*(?:[-•]|\d+\.)\s+/, ""))}</div> : line ? <p key={index}>{bold(line)}</p> : <br key={index} />)}</>;
}

function ScheduleReview({ conversationId, pendingSchedule, setPendingSchedule, timezone, loadCalendar, setImportStatus, importStatus, onClose }) {
  const [classes, setClasses] = useState(pendingSchedule.classes);
  const [saving, setSaving] = useState(false);
  const [termStart, setTermStart] = useState(pendingSchedule.classes[0]?.start_date || "");
  const [termEnd, setTermEnd] = useState(pendingSchedule.classes[0]?.end_date || "");
  useEffect(() => { setClasses(pendingSchedule.classes); setTermStart(pendingSchedule.classes[0]?.start_date || ""); setTermEnd(pendingSchedule.classes[0]?.end_date || ""); }, [pendingSchedule]);
  const updateClass = (index, field, value) => setClasses((old) => old.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: value } : item));
  const updateDay = (classIndex, dayIndex, day) => setClasses((old) => old.map((item, itemIndex) => itemIndex === classIndex ? { ...item, days: item.days.map((currentDay, currentIndex) => currentIndex === dayIndex ? day : currentDay) } : item));
  const addDay = (classIndex) => updateClass(classIndex, "days", [...classes[classIndex].days, "Monday"]);
  const removeDay = (classIndex, dayIndex) => updateClass(classIndex, "days", classes[classIndex].days.filter((_, index) => index !== dayIndex));
  const addMeetingTime = (classIndex) => {
    const meeting = classes[classIndex];
    const additionalMeeting = { ...meeting, days: ["Monday"], start_time: meeting.start_time, end_time: meeting.end_time };
    setClasses((old) => [...old.slice(0, classIndex + 1), additionalMeeting, ...old.slice(classIndex + 1)]);
  };
  const reviewedClasses = () => classes.map((item) => ({ ...item, days: [...new Set(item.days)], start_date: termStart, end_date: termEnd }));
  const request = async (url, options) => {
    const response = await apiFetch(url, options);
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "Something went wrong.");
    return response.json();
  };
  const save = async () => {
    setSaving(true); setImportStatus("");
    try { const result = await request(`/api/schedule/${conversationId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ classes: reviewedClasses() }) }); setPendingSchedule({ classes: result.classes }); setImportStatus(result.message); return true; }
    catch (error) { setImportStatus(error.message); return false; } finally { setSaving(false); }
  };
  const cancel = async () => {
    setSaving(true); setImportStatus("");
    try { const result = await request(`/api/schedule/${conversationId}`, { method: "DELETE" }); setPendingSchedule(null); setImportStatus(result.message); onClose(); }
    catch (error) { setImportStatus(error.message); } finally { setSaving(false); }
  };
  const addClasses = async () => {
    setSaving(true); setImportStatus("");
    try { if (!await save()) return; const result = await request(`/api/schedule/${conversationId}/import?timezone=${encodeURIComponent(timezone)}`, { method: "POST" }); setPendingSchedule(null); setImportStatus(result.message); loadCalendar(); onClose(); }
    catch (error) { setImportStatus(error.message); } finally { setSaving(false); }
  };
  return <section className="schedule-review"><header className="schedule-review-hero"><div><span className="schedule-kicker">CLASS IMPORT</span><h3>Your semester, sorted.</h3></div><b className="class-count">{classes.length}<small>classes</small></b></header><div className="term-dates"><label>Term starts<input type="date" value={termStart} onChange={(event) => setTermStart(event.target.value)} /></label><label>Term ends<input type="date" value={termEnd} onChange={(event) => setTermEnd(event.target.value)} /></label></div><div className="class-grid">{classes.map((item, index) => <div className={`class-review class-card-${index % 3}`} key={`${item.name}-${index}`}><span className="class-number">{String(index + 1).padStart(2, "0")}</span><label>Class<input value={item.name} onChange={(event) => updateClass(index, "name", event.target.value)} /></label><div className="meeting-days"><span>Meeting days</span>{item.days.map((day, dayIndex) => <div className="day-picker" key={`${day}-${dayIndex}`}><select value={day} onChange={(event) => updateDay(index, dayIndex, event.target.value)}>{DAY_OPTIONS.map((option) => <option key={option}>{option}</option>)}</select><button type="button" onClick={() => removeDay(index, dayIndex)} disabled={item.days.length === 1} aria-label="Remove day">×</button></div>)}<button type="button" className="add-day" onClick={() => addDay(index)}>+ Add another day</button></div><div className="time-pair"><label>Starts<select value={item.start_time} onChange={(event) => updateClass(index, "start_time", event.target.value)}>{TIME_OPTIONS.map((option) => <option key={option}>{option}</option>)}</select></label><label>Ends<select value={item.end_time} onChange={(event) => updateClass(index, "end_time", event.target.value)}>{TIME_OPTIONS.map((option) => <option key={option}>{option}</option>)}</select></label></div><label>Location <em>optional</em><input value={item.location || ""} placeholder="e.g. Hall 201" onChange={(event) => updateClass(index, "location", event.target.value)} /></label><button type="button" className="add-meeting" onClick={() => addMeetingTime(index)}>+ Add another class time</button></div>)}</div><p className="review-footnote">Review your classes, then add them to Google Calendar when everything looks right.</p><div className="review-actions"><button className="review-secondary" onClick={cancel} disabled={saving}>Cancel import</button><button className="review-secondary" onClick={save} disabled={saving}>Save details</button><button className="dark" onClick={addClasses} disabled={saving || !termStart || !termEnd}>{saving ? "Working…" : "Add all classes"}</button></div>{importStatus && <p className="import-status">{importStatus}</p>}</section>;
}

function Today({ prompt, compact, selectedDate, setSelectedDate, week, weekStart, setWeekStart, calendar, calendarError, calendarLoading, timezone, loadCalendar, setModal, setActiveEvent }) {
  // This section combines date selection, the live event timeline, and suggestions.
  const selectedKey = dateKey(selectedDate);
  const readableDate = selectedDate.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
  const freeText = calendar.events.length ? "Ask Cal to find the best gap in this schedule." : "Your calendar is open—make room for what matters.";
  return <section className={`today ${compact ? "today-compact" : ""}`}>
    <div className="week">{week.map((day) => <button className={dateKey(day) === selectedKey ? "selected" : ""} key={dateKey(day)} onClick={() => setSelectedDate(day)}><span>{formatDay(day)}</span><b>{day.getDate()}</b></button>)}</div>
    <div className="calendar-grid"><div className="timeline"><header><b><i />{readableDate}</b><span><button aria-label="Previous week" onClick={() => { const next = addDays(weekStart, -7); setWeekStart(next); setSelectedDate(next); }}><ChevronLeft size={17} /></button><button aria-label="Next week" onClick={() => { const next = addDays(weekStart, 7); setWeekStart(next); setSelectedDate(next); }}><ChevronRight size={17} /></button><button aria-label="Refresh calendar" onClick={loadCalendar}><RefreshCw size={15} /></button></span></header>
      {calendarLoading ? <div className="calendar-loading"><i /><i /><i /></div> : calendarError ? <p className="calendar-error">{calendarError}</p> : calendar.events.length ? calendar.events.map((event, index) => <article className={["work", "personal", "health"][index % 3]} key={event.id} onClick={() => { setActiveEvent(event); setModal("event"); }}><time>{event.allDay ? "All day" : formatTime(event.start, timezone)}<small>{event.allDay ? "" : formatTime(event.end, timezone)}</small></time><div><b>{event.title}</b><span>{event.location || event.description || "Google Calendar"}</span></div><MoreHorizontal size={18} /></article>) : <div className="empty-calendar"><Clock3 size={19} /><b>Your day is open.</b><span>Use Cal to plan something meaningful.</span><button onClick={() => prompt("Help me plan this day")}>Plan this day <ChevronRight size={14} /></button></div>}
      {!calendarError && calendar.events.length > 0 && <p className="open-slot"><Clock3 size={18} />{freeText}</p>}
    </div><aside className="ask-cal"><span className="badge"><Sparkles size={14} />ASK CAL</span><h3>Make your time work harder.</h3><p>{calendar.events.length ? "Suggestions shaped around this day." : "Start with a simple planning prompt."}</p>{(calendar.suggestions.length ? calendar.suggestions : ["Help me plan my week", "Find time for a workout this week"]).map((item) => <button key={item} onClick={() => prompt(item)}>{item}<ChevronRight size={15} /></button>)}</aside></div>
  </section>;
}

// Profile changes are saved in localStorage by the App component.
function Profile({ name, setName, timezone, setTimezone, role, setRole, saveProfile, onClose }) { const saveAndClose = async () => { await saveProfile(); onClose(); }; return <section className="profile"><div className="profile-hero"><b className="avatar">{name[0]?.toUpperCase()}</b></div><div className="profile-card"><label>Name<input value={name} onChange={(e) => setName(e.target.value)} /></label><label>Role<select value={role} onChange={e => setRole(e.target.value)}><option value="">Select your role</option><option>College student</option><option>High school student</option><option>Graduate student</option><option>Full-time professional</option><option>Part-time professional</option><option>Freelancer</option><option>Parent or caregiver</option><option>Retired</option><option>Other</option></select></label><label>Home timezone<select value={timezone} onChange={(e) => setTimezone(e.target.value)}>{ZONES.map((zone) => <option key={zone}>{zone}</option>)}</select></label><div className="connected"><svg className="connected-google-logo" viewBox="0 0 18 18" aria-hidden="true"><path fill="#4285F4" d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.482h4.844a4.14 4.14 0 0 1-1.797 2.716v2.258h2.909c1.702-1.567 2.684-3.874 2.684-6.615Z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.909-2.258c-.806.54-1.835.859-3.047.859-2.344 0-4.329-1.585-5.037-3.711H.956v2.333A9 9 0 0 0 9 18Z"/><path fill="#FBBC05" d="M3.963 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.281-1.71V4.957H.956A9 9 0 0 0 0 9c0 1.452.347 2.827.956 4.043l3.007-2.333Z"/><path fill="#EA4335" d="M9 3.579c1.322 0 2.508.454 3.441 1.346l2.581-2.581C13.463.892 11.426 0 9 0A9 9 0 0 0 .956 4.957L3.963 7.29C4.671 5.164 6.656 3.579 9 3.579Z"/></svg><span><strong>Google Calendar</strong></span><em className="connection-status"><i />Connected</em><Check size={18} /></div><button className="dark" onClick={saveAndClose}><Check size={17} />Save profile</button></div></section>; }
function EventEditor({ event, timezone, refresh, close }) {
  const [title, setTitle] = useState(event.title); const [location, setLocation] = useState(event.location); const [description, setDescription] = useState(event.description); const [saving, setSaving] = useState(false); const [warning, setWarning] = useState("");
  const call = async (url, options) => { const r = await apiFetch(url, options); const body = await r.json().catch(() => ({})); if (!r.ok) throw new Error(typeof body.detail === "object" ? `${body.detail.reason}${body.detail.alternatives?.length ? ` Try: ${body.detail.alternatives.join(" or ")}.` : ""}` : body.detail || "Something went wrong."); return body; };
  const save = async () => { setSaving(true); setWarning(""); try { await call(`/api/events/${event.id}?timezone=${encodeURIComponent(timezone)}`, { method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title, location, description, start:event.start, end:event.end}) }); refresh(); close(); } catch (error) { setWarning(error.message); } finally { setSaving(false); } };
  const remove = async () => { if (!confirm(`Delete ${event.title}?`)) return; setSaving(true); try { await call(`/api/events/${event.id}`, {method:"DELETE"}); refresh(); close(); } finally { setSaving(false); } };
  return <section className="event-editor"><header><span className="badge">CALENDAR EVENT</span><h2>Edit your plan</h2><p>{event.allDay ? "All day" : `${formatTime(event.start, timezone)} – ${formatTime(event.end, timezone)}`}</p></header><div className="event-editor-fields"><label>Title<input value={title} onChange={e=>setTitle(e.target.value)} /></label><label>Location<input value={location} onChange={e=>setLocation(e.target.value)} placeholder="Add a location" /></label><label>Description<textarea value={description} onChange={e=>setDescription(e.target.value)} placeholder="Add notes or details" rows="4" /></label></div>{warning && <p className="event-warning">{warning}</p>}<footer><button className="event-delete" onClick={remove} disabled={saving}>Delete event</button><button className="dark" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save changes"}</button></footer></section>;
}
