// Refreshes server-rendered fragments while background work is running.
(() => {
  const INTERVAL_MS = 4000;

  function schedule(element) {
    if (element.dataset.pollActive !== "true") {
      return;
    }
    window.setTimeout(async () => {
      try {
        const response = await fetch(element.dataset.pollUrl, {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!response.ok) {
          window.location.reload();
          return;
        }
        const template = document.createElement("template");
        template.innerHTML = (await response.text()).trim();
        const next = template.content.firstElementChild;
        if (next) {
          // Fragments that cannot show the finished result ask for a full reload.
          if (next.dataset.pollActive !== "true" && next.dataset.pollReload === "true") {
            window.location.reload();
            return;
          }
          element.replaceWith(next);
          schedule(next);
        }
      } catch {
        schedule(element);
      }
    }, INTERVAL_MS);
  }

  document.querySelectorAll("[data-poll-url]").forEach(schedule);
})();
