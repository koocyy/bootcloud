// Live "N online" counter in the tab title, shared by every page on the site.
// Each open tab holds a websocket to /ws/online; the server counts browsers (not
// tabs) by their session cookie and pushes the site-wide count whenever it changes.
(() => {
  const baseTitle = document.title;
  let wantedTitle = baseTitle;

  const applyTitle = () => {
    if (document.title !== wantedTitle) document.title = wantedTitle;
  };

  // games (e.g. the Doom engine) set document.title themselves; put ours back
  new MutationObserver(applyTitle).observe(document.head, { subtree: true, childList: true, characterData: true });

  const connect = (retryDelay = 1000) => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/ws/online`);
    socket.addEventListener("open", () => { retryDelay = 1000; });
    socket.addEventListener("message", (event) => {
      const count = Number(event.data);
      if (!Number.isInteger(count)) return;
      wantedTitle = `${baseTitle} | ${count} online`;
      applyTitle();
    });
    socket.addEventListener("close", (event) => {
      wantedTitle = baseTitle;
      applyTitle();
      // 4001: this browser has no session cookie (the server is limiting new
      // browsers from this network); retrying can't help until the page reloads
      if (event.code === 4001) return;
      setTimeout(() => connect(Math.min(retryDelay * 2, 30000)), retryDelay);
    });
  };
  connect();
})();
