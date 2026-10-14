(() => {
  const editors = [];
  let saveQueue = Promise.resolve();

  document.querySelectorAll("[data-gap-autosave], [data-content-autosave]").forEach((form) => {
    const fields = Array.from(form.querySelectorAll("input:not([type='hidden']), textarea"));
    const status = form.querySelector("[data-save-status]");
    const snapshot = () => JSON.stringify(fields.map((field) => field.value));
    let savedValue = snapshot();
    let saving = false;
    let pending = Promise.resolve();
    let timer;

    function showStatus(message, failed = false) {
      status.hidden = false;
      status.textContent = message;
      status.className = failed ? "error" : "meta";
    }

    function save() {
      window.clearTimeout(timer);
      if (saving) {
        return pending;
      }
      if (snapshot() === savedValue) {
        return Promise.resolve();
      }
      saving = true;
      showStatus("Saving...");
      let succeeded = false;
      const queued = saveQueue.then(async () => {
        const value = snapshot();
        if (value === savedValue) {
          showStatus("Saved");
          succeeded = true;
          return;
        }
        if (!form.reportValidity()) {
          showStatus("Check the highlighted fields.", true);
          return;
        }
        const data = new FormData(form);
        const response = await fetch(form.action, {
          method: "POST",
          body: data,
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          keepalive: true,
        });
        const result = await response.json();
        if (!response.ok || !result.saved) {
          throw new Error(result.message || "Could not save changes.");
        }
        if (form.hasAttribute("data-content-autosave")) {
          if (!/^[0-9a-f]{16}$/.test(result.blob || "")) {
            throw new Error("Could not confirm the saved version. Reload before editing again.");
          }
          document.querySelectorAll('input[name="blob"]').forEach((field) => {
            field.value = result.blob;
          });
          document.querySelectorAll("[data-content-approved]").forEach((element) => {
            element.hidden = true;
          });
          document.querySelectorAll("[data-content-needs-approval], [data-content-approval]").forEach((element) => {
            element.hidden = false;
          });
          const title = form.closest("article").querySelector("[data-item-title]");
          title.textContent = form.elements.namedItem("name")?.value || form.elements.namedItem("question")?.value;
        }
        savedValue = value;
        succeeded = true;
        showStatus("Saved");
      });
      saveQueue = queued.catch(() => {});
      pending = queued.then(
        () => {
          saving = false;
          if (succeeded && snapshot() !== savedValue) {
            return save();
          }
        },
        (error) => {
          saving = false;
          showStatus(error.message || "Could not save changes.", true);
        },
      );
      return pending;
    }

    fields.forEach((field) => {
      field.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(save, 600);
      });
      field.addEventListener("blur", save);
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void save();
    });
    editors.push({ save, hasChanges: () => saving || snapshot() !== savedValue });
  });

  if (editors.length === 0) {
    return;
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (form.matches("[data-gap-autosave], [data-content-autosave]") ||
        !form.querySelector('input[name="blob"]') ||
        !editors.some((editor) => editor.hasChanges())) {
      return;
    }
    event.preventDefault();
    const submitter = event.submitter;
    await Promise.all(editors.map((editor) => editor.save()));
    await saveQueue;
    if (!editors.some((editor) => editor.hasChanges())) {
      form.requestSubmit(submitter);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      editors.forEach((editor) => { void editor.save(); });
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (editors.some((editor) => editor.hasChanges())) {
      editors.forEach((editor) => { void editor.save(); });
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();