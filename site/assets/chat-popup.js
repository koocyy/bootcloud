// Opens links marked data-chat-popup in one small, reusable popup window, so the
// chat stays open beside a game. If popups are blocked, the link opens normally.
document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-chat-popup]");
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

  const width = 420;
  const height = 680;
  const left = Math.max(0, window.screenX + window.outerWidth - width - 24);
  const top = Math.max(0, window.screenY + 80);
  // an empty URL finds the chat window if it is already open, without reloading it
  const popup = window.open("", "bootcloud-chat", `popup,width=${width},height=${height},left=${left},top=${top}`);
  if (!popup) return;

  event.preventDefault();
  if (popup.location.href === "about:blank") popup.location.href = link.href;
  popup.focus();
});
