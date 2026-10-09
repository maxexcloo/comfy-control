const formatDate = (timestamp) =>
  new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: document.body.dataset.timeZone || "Australia/Sydney",
  }).format(new Date(Number(timestamp) * 1000));

const formatTimes = (root = document) => {
  for (const time of root.querySelectorAll("[data-timestamp]"))
    time.textContent = formatDate(time.dataset.timestamp);
};

formatTimes();

for (const control of document.querySelectorAll("[data-current]")) {
  if (control.type === "checkbox") {
    control.checked = control.dataset.current === "True";
  } else {
    control.value = control.dataset.current;
  }
}

for (const button of document.querySelectorAll("[data-confirmation]")) {
  button.closest("form").addEventListener("submit", (event) => {
    if (!confirm(button.dataset.confirmation)) event.preventDefault();
  });
}

for (const form of document.querySelectorAll("[data-live-filter]")) {
  let timer;
  const submit = () => {
    clearTimeout(timer);
    form.requestSubmit();
  };
  form.addEventListener("input", (event) => {
    if (event.target.matches('input[type="search"], input[name="q"]')) {
      clearTimeout(timer);
      timer = setTimeout(() => form.requestSubmit(), 300);
    }
  });
  form.addEventListener("change", submit);
}

for (const editor of document.querySelectorAll("[data-route-editor]")) {
  const value = editor.querySelector("[data-route-value]");
  const syncModel = (target) => {
    const provider = target.querySelector("[data-route-provider]").value;
    const model = target.querySelector("[data-route-model]");
    for (const option of model.options) {
      const available = option.dataset.providers.trim().split(/\s+/);
      option.disabled = !available.includes(provider);
      option.hidden = option.disabled;
    }
    if (model.selectedOptions[0]?.disabled)
      model.value = [...model.options].find(
        (option) => !option.disabled,
      )?.value;
  };
  const serialise = () => {
    for (const target of editor.querySelectorAll("[data-route-target]"))
      syncModel(target);
    value.value = JSON.stringify(
      Object.fromEntries(
        [...editor.querySelectorAll("[data-route-key]")].map((route) => [
          route.dataset.routeKey,
          [...route.querySelectorAll("[data-route-target]")].map((target) => ({
            model: target.querySelector("[data-route-model]").value,
            provider: target.querySelector("[data-route-provider]").value,
          })),
        ]),
      ),
    );
    const selectedModels = new Set(
      [...editor.querySelectorAll("[data-route-model]")].map(
        (select) => select.value,
      ),
    );
    for (const model of selectedModels) {
      const installation = document.querySelector(
        `[data-model-package="${CSS.escape(model)}"]`,
      );
      if (installation && !installation.disabled) installation.checked = true;
    }
    for (const route of editor.querySelectorAll("[data-route-key]")) {
      const selected = [...route.querySelectorAll("[data-route-provider]")].map(
        (select) => select.value,
      );
      for (const select of route.querySelectorAll("[data-route-provider]")) {
        for (const option of select.options)
          option.disabled =
            option.value !== select.value && selected.includes(option.value);
      }
    }
  };
  let dragged;
  editor.addEventListener("dragstart", (event) => {
    dragged = event.target.closest("[data-route-target]");
    if (dragged) event.dataTransfer.effectAllowed = "move";
  });
  editor.addEventListener("dragover", (event) => {
    const target = event.target.closest("[data-route-target]");
    if (!dragged || !target || target === dragged) return;
    event.preventDefault();
    const bounds = target.getBoundingClientRect();
    target.parentElement.insertBefore(
      dragged,
      event.clientY < bounds.top + bounds.height / 2
        ? target
        : target.nextSibling,
    );
  });
  editor.addEventListener("dragend", () => {
    dragged = undefined;
    serialise();
  });
  editor.addEventListener("change", serialise);
  editor.addEventListener("click", (event) => {
    const route = event.target.closest("[data-route-key]");
    if (!route) return;
    const target = event.target.closest("[data-route-target]");
    if (event.target.closest("[data-route-add]")) {
      const row = editor
        .querySelector(`[data-route-template="${route.dataset.routeKey}"]`)
        .content.firstElementChild.cloneNode(true);
      const used = new Set(
        [...route.querySelectorAll("[data-route-provider]")].map(
          (select) => select.value,
        ),
      );
      const select = row.querySelector("[data-route-provider]");
      const available = [...select.options].find(
        (option) => !used.has(option.value),
      );
      if (!available) return;
      select.value = available.value;
      route.querySelector("[data-route-list]").append(row);
    } else if (event.target.closest("[data-route-remove]")) {
      target.remove();
    } else if (event.target.closest("[data-route-up]")) {
      if (target.previousElementSibling)
        target.parentElement.insertBefore(
          target,
          target.previousElementSibling,
        );
    } else if (event.target.closest("[data-route-down]")) {
      if (target.nextElementSibling)
        target.parentElement.insertBefore(target.nextElementSibling, target);
    } else {
      return;
    }
    serialise();
  });
  serialise();
}

const closeDialogOnBackdrop = (dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
};

for (const button of document.querySelectorAll("[data-provider-settings]")) {
  const dialog = document.getElementById(
    `provider-settings-${button.dataset.providerSettings}`,
  );
  button.addEventListener("click", () => dialog.showModal());
  for (const close of dialog.querySelectorAll("[data-close]"))
    close.addEventListener("click", () => dialog.close());
  closeDialogOnBackdrop(dialog);
}

const logDialog = document.getElementById("log-dialog");
if (logDialog) {
  const logBody = logDialog.querySelector(".dialog-body");
  const logFollowState = document.getElementById("log-follow-state");
  const logOutput = document.getElementById("log-output");
  let following = true;
  let logSource;
  const updateLogFollowState = () => {
    logFollowState.textContent = following ? "Following" : "Paused";
  };
  const scrollLogsToBottom = () => {
    if (!following) return;
    requestAnimationFrame(() => {
      logBody.scrollTop = logBody.scrollHeight;
    });
  };
  logBody.addEventListener("scroll", () => {
    following =
      logBody.scrollHeight - logBody.scrollTop - logBody.clientHeight <= 24;
    updateLogFollowState();
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-log-url]");
    if (!button) return;
    event.preventDefault();
    if (logSource) logSource.close();
    document.getElementById("log-title").textContent =
      `${button.dataset.logProvider} logs`;
    following = true;
    updateLogFollowState();
    logOutput.textContent = "Connecting…";
    logDialog.showModal();
    scrollLogsToBottom();
    logSource = new EventSource(button.dataset.logUrl);
    logSource.onmessage = (message) => {
      const data = JSON.parse(message.data);
      const lines = [...data.entries]
        .reverse()
        .map(
          (item) =>
            `${formatDate(item.created_at)} ${item.source || "Provider"} ${item.level.toUpperCase()} ${item.message}${item.request_id ? ` [${item.request_id}]` : ""}`,
        );
      if (data.worker_error)
        lines.push(`Worker logs unavailable: ${data.worker_error}`);
      logOutput.textContent = lines.join("\n");
      scrollLogsToBottom();
    };
    logSource.onerror = () => {
      if (!logOutput.textContent) logOutput.textContent = "Waiting for logs…";
    };
  });
  logDialog
    .querySelector("[data-close]")
    .addEventListener("click", () => logDialog.close());
  logDialog.addEventListener("close", () => {
    if (logSource) {
      logSource.close();
      logSource = undefined;
    }
  });
  closeDialogOnBackdrop(logDialog);
}

const deployDialog = document.getElementById("deploy-dialog");
if (deployDialog) {
  const deployDetail = document.getElementById("deploy-option-detail");
  const deployForm = document.getElementById("deploy-form");
  const deployGpu = document.getElementById("deploy-gpu");
  const deployMemory = document.getElementById("deploy-memory");
  const deployOption = document.getElementById("deploy-option");
  const deployVariant = document.getElementById("deploy-variant");
  let deploymentOptions = [];
  const updateDeployDetail = () => {
    const option = deploymentOptions.find(
      (item) => item.id === deployGpu.value,
    );
    deployMemory.value = option?.memory_gb || "";
    deployOption.value = option?.provider_option_id || option?.id || "";
    deployVariant.value = option?.variant || "";
    if (!option) {
      deployDetail.textContent = "The provider will choose automatically.";
      return;
    }
    const details = [
      option.type
        ? option.type
            .replaceAll("-", " ")
            .replace(/\b\w/g, (letter) => letter.toUpperCase())
        : null,
      option.available ? "Available" : "Currently Unavailable",
      option.availability ? `${option.availability} Stock` : null,
      option.compatible ? "Model Compatible" : "Insufficient GPU Memory",
      option.memory_gb ? `${option.memory_gb} GB GPU Memory` : null,
      Number.isFinite(option.compute_capability)
        ? `Compute ${Number(option.compute_capability) / 100}`
        : null,
      Number.isFinite(option.cuda_maximum)
        ? `CUDA ${option.cuda_maximum}`
        : null,
      option.cpu_cores ? `${option.cpu_cores} vCPU` : null,
      option.system_memory_gb
        ? `${option.system_memory_gb} GB System Memory`
        : null,
      option.cloud ? `${option.cloud} Cloud` : null,
      option.location || null,
      Number.isFinite(option.reliability)
        ? `${(Number(option.reliability) * 100).toFixed(1)}% Reliability`
        : null,
      Number.isFinite(option.cost_per_hour)
        ? `$${Number(option.cost_per_hour).toFixed(3)} Per Hour`
        : "Provider-Managed Pricing",
    ].filter(Boolean);
    deployDetail.textContent = details.join(" · ");
  };
  for (const button of document.querySelectorAll("[data-deploy-provider]")) {
    button.addEventListener("click", async () => {
      document.getElementById("deploy-title").textContent =
        `Deploy ${button.dataset.deployProvider}`;
      deployForm.action = button.dataset.deployAction;
      deployGpu.replaceChildren(new Option("Loading Availability…", ""));
      deployGpu.disabled = true;
      deployDialog.showModal();
      const response = await fetch(button.dataset.deployOptions);
      if (!response.ok) {
        deployDetail.textContent = "Live availability could not be loaded.";
        deployGpu.replaceChildren(new Option("Automatic", ""));
        deployGpu.disabled = false;
        return;
      }
      deploymentOptions = (await response.json()).options;
      deployGpu.replaceChildren(
        new Option("Automatic (Recommended)", ""),
        ...deploymentOptions.map((item) => {
          const option = new Option(
            `${item.label}${item.cloud ? ` (${item.cloud})` : ""}${Number.isFinite(item.cost_per_hour) ? ` · $${Number(item.cost_per_hour).toFixed(3)}/h` : ""}`,
            item.id,
            false,
            false,
          );
          option.disabled = !item.available || !item.compatible;
          return option;
        }),
      );
      deployGpu.disabled = false;
      updateDeployDetail();
    });
  }
  deployGpu.addEventListener("change", updateDeployDetail);
  for (const button of deployDialog.querySelectorAll("[data-close]"))
    button.addEventListener("click", () => deployDialog.close());
  closeDialogOnBackdrop(deployDialog);
}

const addFilter = document.getElementById("add-filter");
if (addFilter) {
  addFilter.addEventListener("click", () => {
    const path = document.getElementById("filter-path").value;
    const operator = document.getElementById("filter-operator").value;
    const value = document.getElementById("filter-value").value.trim();
    if (!path || !value) return;
    const chip = document.createElement("span");
    chip.className = "filter-chip";
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "filter";
    input.value = `${path}|${operator}|${value}`;
    chip.append(input, `${path} ${operator.replaceAll("_", " ")} ${value} `);
    const remove = document.createElement("button");
    remove.className = "btn";
    remove.dataset.variant = "link";
    remove.dataset.size = "sm";
    remove.type = "button";
    remove.dataset.removeFilter = "";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Remove Filter");
    chip.append(remove);
    document.getElementById("filter-chips").append(chip);
    document.getElementById("filter-value").value = "";
    document.getElementById("media-search").requestSubmit();
  });
  document.getElementById("filter-chips").addEventListener("click", (event) => {
    if (event.target.matches("[data-remove-filter]")) {
      event.target.closest(".filter-chip").remove();
      document.getElementById("media-search").requestSubmit();
    }
  });
}

const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const labelledValue = (label, value) => {
  const item = element("div", "card p-3");
  item.append(
    element("small", "block text-muted-foreground", label),
    element("strong", "block", value || "—"),
  );
  return item;
};

const formatBytes = (bytes) => {
  if (!Number.isFinite(bytes) || bytes < 1024) return `${bytes || 0} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = "B";
  for (const candidate of units) {
    value /= 1024;
    unit = candidate;
    if (value < 1024) break;
  }
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)} ${unit}`;
};

const formatDuration = (seconds) => {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
};

const mediaKind = (contentType) => {
  const [kind, format] = String(contentType || "media").split("/", 2);
  return `${kind.charAt(0).toUpperCase()}${kind.slice(1)}${format ? ` · ${format.toUpperCase()}` : ""}`;
};

const titleText = (value) =>
  String(value || "")
    .replaceAll(/[_-]+/g, " ")
    .replaceAll(/\b\w/g, (character) => character.toUpperCase());

const mediaDialog = document.getElementById("media-dialog");
if (mediaDialog) {
  const actionDialog = document.getElementById("media-action-dialog");
  const actionError = document.getElementById("media-action-error");
  const actionForm = document.getElementById("media-action-form");
  const actionModel = document.getElementById("media-action-model");
  const actionProvider = document.getElementById("media-action-provider");
  const actionScale = document.getElementById("media-action-scale");
  const actionSubmit = document.getElementById("media-action-submit");
  const detailRoot = document.getElementById("media-detail");
  const nextButton = mediaDialog.querySelector("[data-next-media]");
  const previousButton = mediaDialog.querySelector("[data-previous-media]");
  let activeAssetId = null;
  let activeItem;
  let activeMediaAction;
  let mediaNotice;
  let openRequest = 0;
  const providerLabel = (provider) => provider || "—";
  const updateActionProviders = () => {
    const selected = activeItem.actions[activeMediaAction].find(
      (model) => model.id === actionModel.value,
    );
    actionProvider.replaceChildren(element("option", "", "Automatic"));
    actionProvider.firstElementChild.value = "";
    for (const provider of selected?.providers || []) {
      const option = element("option", "", providerLabel(provider));
      option.value = provider;
      actionProvider.append(option);
    }
  };
  const updateUpscaleSummary = () => {
    const scale = Number(actionScale.value);
    const summary = document.getElementById("media-upscale-summary");
    if (!activeItem.width || !activeItem.height) {
      summary.textContent = `${scale * scale}× the source pixel count`;
      return;
    }
    summary.textContent = `${activeItem.width} × ${activeItem.height} → ${Math.round(activeItem.width * scale)} × ${Math.round(activeItem.height * scale)} · ${scale * scale}× pixels`;
  };
  const openMediaAction = (action) => {
    activeMediaAction = action;
    const editing = action === "image_edit";
    const label = editing ? "Edit Image" : "Upscale Image";
    document.getElementById("media-action-kind").textContent = mediaKind(
      activeItem.content_type,
    );
    document.getElementById("media-action-title").textContent = label;
    actionSubmit.textContent = label;
    actionError.classList.add("hidden");
    actionError.textContent = "";
    for (const field of actionDialog.querySelectorAll("[data-edit-field]"))
      field.classList.toggle("hidden", !editing);
    for (const field of actionDialog.querySelectorAll("[data-upscale-field]"))
      field.classList.toggle("hidden", editing);
    document.getElementById("media-action-prompt").required = editing;
    actionModel.replaceChildren();
    for (const model of activeItem.actions[action]) {
      const option = element("option", "", model.id);
      option.value = model.id;
      actionModel.append(option);
    }
    updateActionProviders();
    updateUpscaleSummary();
    actionDialog.showModal();
  };
  actionModel.addEventListener("change", updateActionProviders);
  actionScale.addEventListener("change", updateUpscaleSummary);
  for (const close of actionDialog.querySelectorAll("[data-close]"))
    close.addEventListener("click", () => actionDialog.close());
  closeDialogOnBackdrop(actionDialog);
  actionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(actionForm);
    form.append("csrf_token", actionDialog.dataset.csrfToken);
    const action = activeMediaAction === "image_edit" ? "edit" : "upscale";
    actionSubmit.disabled = true;
    actionSubmit.setAttribute("aria-busy", "true");
    actionError.classList.add("hidden");
    try {
      const response = await fetch(`/media/${activeAssetId}/${action}`, {
        body: form,
        method: "POST",
      });
      const result = await response.json();
      if (!response.ok)
        throw new Error(result.error?.message || "The Image Action Failed");
      mediaNotice = `${activeMediaAction === "image_edit" ? "Image edited" : "Image upscaled"}${result.provider ? ` with ${providerLabel(result.provider)}` : ""}`;
      actionDialog.close();
      await openMedia(result.asset_id);
    } catch (error) {
      actionError.textContent = error.message;
      actionError.classList.remove("hidden");
    } finally {
      actionSubmit.disabled = false;
      actionSubmit.removeAttribute("aria-busy");
    }
  });
  const availableMediaIds = () => [
    ...new Set(
      [...document.querySelectorAll("[data-media-id]:not(.related-item)")].map(
        (item) => item.dataset.mediaId,
      ),
    ),
  ];
  const updateNavigation = () => {
    const ids = availableMediaIds();
    const index = ids.indexOf(activeAssetId);
    const results = document.getElementById("media-results");
    const page = Number(results?.dataset.mediaPage || 1);
    const pages = Number(results?.dataset.mediaPages || 1);
    previousButton.disabled = index <= 0 && page <= 1;
    nextButton.disabled = index >= ids.length - 1 && page >= pages;
  };
  const openMedia = async (assetId, updateAddress = true) => {
    activeAssetId = String(assetId);
    for (const card of document.querySelectorAll(".media-selected"))
      card.classList.remove("media-selected");
    const request = ++openRequest;
    updateNavigation();
    detailRoot.replaceChildren(element("p", "", "Loading…"));
    if (!mediaDialog.open) mediaDialog.showModal();
    const response = await fetch(`/media/${assetId}`);
    if (request !== openRequest) return;
    if (!response.ok) {
      detailRoot.replaceChildren(
        element("p", "text-destructive", "Media could not be loaded."),
      );
      return;
    }
    const item = await response.json();
    activeItem = item;
    const use = item.primary_use || {};
    const actualModel = use.provider_model || use.model;
    document.getElementById("media-kind").textContent = mediaKind(
      item.content_type,
    );
    document.getElementById("media-title").textContent =
      actualModel || `Media ${item.id}`;
    const preview = item.content_type.startsWith("video/")
      ? element("video", "media-preview")
      : element("img", "media-preview");
    preview.src = `/media/${item.id}/content`;
    if (preview.tagName === "VIDEO") preview.controls = true;
    else preview.alt = use.prompt || "Generated media";

    const facts = element("div", "detail-grid");
    const factItems = [
      labelledValue("Provider", providerLabel(use.provider)),
      labelledValue("Model", actualModel),
      labelledValue(
        "Dimensions",
        item.width && item.height ? `${item.width} × ${item.height}` : "—",
      ),
      labelledValue("Size", formatBytes(item.size)),
    ];
    if (Number.isFinite(use.generation_seconds))
      factItems.push(
        labelledValue(
          "Generation time",
          formatDuration(use.generation_seconds),
        ),
      );
    facts.append(...factItems);
    const body = document.createDocumentFragment();
    if (mediaNotice) {
      const notice = element("div", "alert");
      notice.append(element("section", "", mediaNotice));
      body.append(notice);
      mediaNotice = undefined;
    }
    body.append(preview);
    const reuseActions = element("div", "media-detail-actions");
    const regenerate = element("a", "btn", "Use in Generate");
    regenerate.dataset.size = "sm";
    regenerate.href = `/generate?source=${encodeURIComponent(item.id)}`;
    reuseActions.append(regenerate);
    body.append(reuseActions);
    if (item.content_type.startsWith("image/")) {
      const actions = element(
        "div",
        "flex flex-wrap items-center justify-between gap-2 my-3",
      );
      const actionButtons = element("div", "flex flex-wrap gap-2");
      for (const [operation, label] of [
        ["image_edit", "Edit Image"],
        ["image_upscale", "Upscale Image"],
      ]) {
        if (!item.actions[operation]?.length) continue;
        const button = element("button", "btn", label);
        button.dataset.size = "sm";
        button.dataset.variant =
          operation === "image_upscale" ? "primary" : "outline";
        button.dataset.mediaAction = operation;
        button.type = "button";
        actionButtons.append(button);
      }
      if (actionButtons.childElementCount) {
        actions.append(
          element("strong", "text-muted-foreground", "Image Actions"),
          actionButtons,
        );
        body.append(actions);
      }
    }
    body.append(facts);
    for (const generation of item.uses) {
      const section = element("section", "card p-4 detail-section");
      const heading = element("div", "flex items-center justify-between");
      const operation = titleText(generation.operation);
      heading.append(
        element(
          "h3",
          "",
          generation.role === "input"
            ? `Used for ${operation}`
            : `${operation} details`,
        ),
      );
      const historyLink = element("a", "", "View History");
      historyLink.href = `/history?q=${encodeURIComponent(generation.history_id)}`;
      const links = element("div", "flex gap-2");
      if (generation.source_url) {
        const sourceLink = element("a", "", "View Source");
        sourceLink.href = generation.source_url;
        sourceLink.rel = "noopener noreferrer";
        sourceLink.target = "_blank";
        links.append(sourceLink);
      }
      links.append(historyLink);
      heading.append(links);
      section.append(heading);
      if (generation.prompt) {
        section.append(
          element("small", "", "Prompt"),
          element("p", "prompt", generation.prompt),
        );
      }
      const parameterDetails = element("details");
      parameterDetails.append(element("summary", "", "Parameters"));
      const parameters = element(
        "pre",
        "parameters",
        JSON.stringify(generation.parameters, null, 2),
      );
      parameterDetails.append(parameters);
      section.append(parameterDetails);
      body.append(section);
    }
    const related = [
      ...item.lineage.sources.map((entry) => ["Source", entry]),
      ...item.lineage.derivatives.map((entry) => ["Derivative", entry]),
    ];
    if (related.length) {
      const section = element("section", "card p-4 detail-section");
      section.append(element("h3", "", "Related Media"));
      const list = element("div", "flex flex-wrap gap-2");
      for (const [relationship, entry] of related) {
        const button = element(
          "button",
          "btn related-item",
          `${relationship}: ${entry.filename || `Media ${entry.id}`}`,
        );
        button.type = "button";
        button.dataset.variant = "outline";
        button.dataset.size = "sm";
        button.dataset.mediaId = entry.id;
        list.append(button);
      }
      section.append(list);
      body.append(section);
    }
    detailRoot.replaceChildren(body);
    updateNavigation();
    if (updateAddress) {
      const url = new URL(window.location);
      url.searchParams.set("asset", item.id);
      history.pushState({ asset: item.id }, "", url);
    }
  };
  const loadMediaPage = async (page) => {
    const url = new URL(window.location);
    url.searchParams.set("page", page);
    url.searchParams.delete("asset");
    const response = await fetch(url);
    if (!response.ok) return false;
    const documentPage = new DOMParser().parseFromString(
      await response.text(),
      "text/html",
    );
    const nextResults = documentPage.getElementById("media-results");
    const currentResults = document.getElementById("media-results");
    if (!nextResults || !currentResults) return false;
    currentResults.replaceWith(nextResults);
    formatTimes(nextResults);
    history.replaceState({}, "", url);
    return true;
  };
  const browseMedia = async (direction) => {
    const ids = availableMediaIds();
    const currentIndex = ids.indexOf(activeAssetId);
    const targetIndex = currentIndex + direction;
    if (targetIndex >= 0 && targetIndex < ids.length) {
      openMedia(ids[targetIndex]);
      return;
    }
    const results = document.getElementById("media-results");
    const page = Number(results?.dataset.mediaPage || 1);
    const pages = Number(results?.dataset.mediaPages || 1);
    const targetPage = page + direction;
    if (
      targetPage < 1 ||
      targetPage > pages ||
      !(await loadMediaPage(targetPage))
    )
      return;
    const nextIds = availableMediaIds();
    if (nextIds.length) openMedia(direction > 0 ? nextIds[0] : nextIds.at(-1));
  };
  document.addEventListener("click", (event) => {
    const action = event.target.closest("[data-media-action]");
    if (action) {
      openMediaAction(action.dataset.mediaAction);
      return;
    }
    const target = event.target.closest("[data-media-id]");
    if (target) openMedia(target.dataset.mediaId);
  });
  mediaDialog
    .querySelector("[data-close]")
    .addEventListener("click", () => mediaDialog.close());
  previousButton.addEventListener("click", () => browseMedia(-1));
  nextButton.addEventListener("click", () => browseMedia(1));
  closeDialogOnBackdrop(mediaDialog);
  document.addEventListener("keydown", (event) => {
    if (!mediaDialog.open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      mediaDialog.close();
      return;
    }
    if (event.target.matches("input, select, textarea, [contenteditable]"))
      return;
    if (event.key === "ArrowLeft") browseMedia(-1);
    if (event.key === "ArrowRight") browseMedia(1);
  });
  mediaDialog.addEventListener("close", () => {
    const selectedAssetId = activeAssetId;
    activeAssetId = null;
    openRequest += 1;
    const url = new URL(window.location);
    if (url.searchParams.has("asset")) {
      url.searchParams.delete("asset");
      history.pushState({}, "", url);
    }
    const selected = document.querySelector(
      `[data-media-id="${CSS.escape(selectedAssetId || "")}"]:not(.related-item)`,
    );
    if (selected) {
      selected.classList.add("media-selected");
      selected.focus({ preventScroll: true });
      selected.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });
  const initialAsset = new URL(window.location).searchParams.get("asset");
  updateNavigation();
  if (initialAsset) openMedia(initialAsset, false);
}

for (const form of document.querySelectorAll(".studio-layout")) {
  const updateModelOptions = () => {
    const kind = form.querySelector("[data-model-kind]:checked")?.dataset
      .modelKind;
    for (const field of form.querySelectorAll("[data-image-option]"))
      field.hidden = kind !== "image";
  };
  form.addEventListener("change", (event) => {
    if (event.target.matches("[data-model-kind]")) updateModelOptions();
  });
  updateModelOptions();
}
