const API_BASE = "http://127.0.0.1:8001";
const RESET_ENDPOINT = "/admin/reset-data";

const $ = (id) => document.getElementById(id);

let contactsCache = [];
let templatesCache = [];
let campaignsCache = [];

const selectedContactIds = new Set();
const scheduleSelectedContactIds = new Set();

// -------------------- API HELPERS --------------------

async function apiRequest(path, options = {}) {
  const headers = { ...(options.headers || {}) };

  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  let response;

  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      credentials: "include",
      headers,
    });
  } catch (error) {
    throw new Error(
      `Cannot connect to backend at ${API_BASE}. Check that Uvicorn is running and CORS is configured.`
    );
  }

  const text = await response.text();
  let data = null;

  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }

  if (!response.ok) {
    const detail =
      data?.detail ||
      data?.message ||
      `Request failed with status ${response.status}`;

    throw new Error(
      typeof detail === "string" ? detail : JSON.stringify(detail)
    );
  }

  return data;
}

function getList(result, key) {
  if (Array.isArray(result)) return result;
  if (Array.isArray(result?.[key])) return result[key];
  if (Array.isArray(result?.items)) return result.items;
  if (Array.isArray(result?.data)) return result.data;
  return [];
}

function showNotice(message, isError = false) {
  const notice = $("notice");

  if (!notice) {
    console[isError ? "error" : "log"](message);
    return;
  }

  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.classList.toggle("success", !isError);
  notice.classList.add("show");
}

function showError(error, prefix = "Something went wrong") {
  console.error(prefix, error);
  showNotice(`${prefix}: ${error.message || error}`, true);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function setMessage(id, message, isError = false) {
  const element = $(id);
  if (!element) return;

  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.toggle("success", !isError);
}

// -------------------- NAVIGATION --------------------

function showSection(sectionId) {
  document.querySelectorAll(".section").forEach((section) => {
    section.classList.toggle("active", section.id === sectionId);
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.section === sectionId);
  });

  if (sectionId === "contacts") {
    loadSavedContacts().catch((error) =>
      showError(error, "Could not load contacts")
    );
  }

  if (sectionId === "templates") {
    loadTemplates().catch((error) =>
      showError(error, "Could not load templates")
    );
  }

  if (sectionId === "campaigns") {
    Promise.all([loadCampaigns(), loadContacts()]).catch((error) =>
      showError(error, "Could not load campaign data")
    );
  }
}

// -------------------- DASHBOARD --------------------

async function loadDashboard() {
  const [contactsResult, templatesResult, campaignsResult] =
    await Promise.all([
      apiRequest("/contacts?skip=0&limit=100"),
      apiRequest("/templates"),
      apiRequest("/campaigns"),
    ]);

  const contacts = getList(contactsResult, "contacts");
  const templates = getList(templatesResult, "templates");
  const campaigns = getList(campaignsResult, "campaigns");

  if ($("contactCount")) {
    $("contactCount").textContent =
      contactsResult?.count ?? contacts.length;
  }

  if ($("templateCount")) {
    $("templateCount").textContent =
      templatesResult?.count ?? templates.length;
  }

  if ($("campaignCount")) {
    $("campaignCount").textContent =
      campaignsResult?.count ?? campaigns.length;
  }
}

// -------------------- GMAIL CONNECTION --------------------

async function checkGmailStatus() {
  const badge = $("connectionBadge");
  const status = $("gmailStatus");
  const button = $("connectGmailBtn");

  if (badge) badge.textContent = "Checking...";
  if (status) status.textContent = "Checking Gmail connection...";
  if (button) button.disabled = true;

  try {
    const result = await apiRequest("/auth/gmail/status");
    const connected = result?.gmail_connected === true;

    if (badge) {
      badge.textContent = connected ? "Connected" : "Not connected";
    }

    if (status) {
      status.textContent = connected
        ? `Gmail connected successfully${result.email ? `: ${result.email}` : "."}`
        : "Connect your Gmail account to enable email sending.";
    }

    if (button) {
      button.textContent = connected ? "Gmail Connected" : "Connect Gmail";
      button.disabled = connected;
    }
  } catch (error) {
    if (badge) badge.textContent = "Unavailable";

    if (status) {
      status.textContent =
        `Could not verify Gmail connection: ${error.message}`;
    }

    if (button) {
      button.textContent = "Connect Gmail";
      button.disabled = false;
    }

    console.error("Gmail status check failed:", error);
  }
}

function openAuthPage() {
  const authUrl = `${API_BASE}/auth/gmail/connect`;

  if (typeof chrome !== "undefined" && chrome.tabs?.create) {
    chrome.tabs.create({ url: authUrl });
  } else {
    window.open(authUrl, "_blank", "noopener,noreferrer");
  }
}

// -------------------- CONTACTS --------------------

async function loadContacts() {
  const result = await apiRequest("/contacts?skip=0&limit=100");

  contactsCache = getList(result, "contacts");

  // Remove deleted contact IDs from the selections.
  const validIds = new Set(contactsCache.map((contact) => Number(contact.id)));

  for (const id of selectedContactIds) {
    if (!validIds.has(id)) selectedContactIds.delete(id);
  }

  for (const id of scheduleSelectedContactIds) {
    if (!validIds.has(id)) scheduleSelectedContactIds.delete(id);
  }

  // Remove unsubscribed contacts from both selections.
  contactsCache
    .filter((contact) => contact.is_unsubscribed)
    .forEach((contact) => {
      selectedContactIds.delete(Number(contact.id));
      scheduleSelectedContactIds.delete(Number(contact.id));
    });

  renderRecipientOptions();
  renderScheduleRecipientOptions();

  updateSelectedContactCount();
  updateScheduleSelectedContactCount();

  return contactsCache;
}

async function loadSavedContacts() {
  const container = $("savedContactsList");

  if (container) container.textContent = "Loading contacts...";

  try {
    await loadContacts();
    renderSavedContacts();
  } catch (error) {
    if (container) {
      container.textContent = `Could not load contacts: ${error.message}`;
    }
    throw error;
  }
}

function renderSavedContacts() {
  const container = $("savedContactsList");
  if (!container) return;

  container.replaceChildren();

  let searchInput = $("contactSearchInput");
  let companyFilter = $("contactCompanyFilter");

  if (!searchInput || !companyFilter) {
    const controls = document.createElement("div");
    controls.className = "contact-filters";

    searchInput = document.createElement("input");
    searchInput.id = "contactSearchInput";
    searchInput.type = "search";
    searchInput.placeholder = "Search by name or email...";
    searchInput.setAttribute(
      "aria-label",
      "Search contacts by name or email"
    );

    companyFilter = document.createElement("select");
    companyFilter.id = "contactCompanyFilter";
    companyFilter.setAttribute(
      "aria-label",
      "Filter contacts by company"
    );

    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "All companies";
    companyFilter.appendChild(allOption);

    controls.append(searchInput, companyFilter);
    container.parentElement.insertBefore(controls, container);

    searchInput.addEventListener("input", renderSavedContacts);
    companyFilter.addEventListener("change", renderSavedContacts);
  }

  const selectedCompany = companyFilter.value;

  const companies = [
    ...new Set(
      contactsCache
        .map((contact) => (contact.company || "").trim())
        .filter(Boolean)
    ),
  ].sort((a, b) => a.localeCompare(b));

  companyFilter.replaceChildren();

  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = "All companies";
  companyFilter.appendChild(allOption);

  companies.forEach((companyName) => {
    const option = document.createElement("option");
    option.value = companyName;
    option.textContent = companyName;
    companyFilter.appendChild(option);
  });

  companyFilter.value = companies.includes(selectedCompany)
    ? selectedCompany
    : "";

  const query = searchInput.value.trim().toLowerCase();
  const companySelection = companyFilter.value;

  const filteredContacts = contactsCache.filter((contact) => {
    const name = [
      contact.first_name,
      contact.last_name,
    ].filter(Boolean).join(" ");

    const matchesSearch = [
      name,
      contact.email || "",
      contact.company || "",
    ].some((value) => value.toLowerCase().includes(query));

    const matchesCompany =
      !companySelection ||
      (contact.company || "").trim() === companySelection;

    return matchesSearch && matchesCompany;
  });

  if (!filteredContacts.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = contactsCache.length
      ? "No contacts match your search or filter."
      : "No contacts saved yet.";

    container.appendChild(empty);
    return;
  }

  filteredContacts.forEach((contact) => {
    const card = document.createElement("article");
    card.className = "record-card";

    const title = document.createElement("h4");
    title.textContent =
      [contact.first_name, contact.last_name].filter(Boolean).join(" ") ||
      contact.email ||
      `Contact ${contact.id}`;

    const email = document.createElement("p");
    email.textContent = contact.email || "No email address";

    const company = document.createElement("p");
    company.textContent = `Company: ${contact.company || "—"}`;

    const actions = document.createElement("div");
    actions.className = "record-actions";

    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.className = "button small edit";
    editButton.textContent = "Edit";
    editButton.addEventListener("click", () =>
      beginEditContact(contact)
    );

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "button small danger";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", () =>
      deleteContact(contact, deleteButton)
    );

    actions.append(editButton, deleteButton);
    card.append(title, email, company, actions);
    container.appendChild(card);
  });
}

async function addContact(event) {
  event.preventDefault();

  const form = $("addContactForm");
  const button = form?.querySelector('button[type="submit"]');

  const payload = {
    email: $("newContactEmail")?.value.trim(),
    first_name: $("newContactFirstName")?.value.trim() || "",
    last_name: $("newContactLastName")?.value.trim() || "",
    company: $("newContactCompany")?.value.trim() || "",
  };

  if (!payload.email) {
    showNotice("Enter a contact email address.", true);
    return;
  }

  if (button) {
    button.disabled = true;
    button.textContent = "Adding...";
  }

  try {
    await apiRequest("/contacts", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    form.reset();
    setMessage("addContactMessage", "Contact added successfully.");

    await Promise.all([loadSavedContacts(), loadDashboard()]);
    showNotice("Contact added successfully.");
  } catch (error) {
    setMessage("addContactMessage", error.message, true);
    showError(error, "Could not add contact");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Add Contact";
    }
  }
}

function beginEditContact(contact) {
  const form = $("editContactForm");

  if (!form) {
    showNotice("Edit contact form is missing from popup.html.", true);
    return;
  }

  $("editContactId").value = contact.id;
  $("editContactEmail").value = contact.email || "";
  $("editContactFirstName").value = contact.first_name || "";
  $("editContactLastName").value = contact.last_name || "";
  $("editContactCompany").value = contact.company || "";

  setMessage("editContactMessage", "");
  form.hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function saveEditedContact(event) {
  event.preventDefault();

  const form = $("editContactForm");
  const button = form?.querySelector('button[type="submit"]');
  const id = $("editContactId")?.value;

  if (!id) {
    showNotice("Select a contact to edit.", true);
    return;
  }

  const payload = {
    email: $("editContactEmail").value.trim(),
    first_name: $("editContactFirstName").value.trim(),
    last_name: $("editContactLastName").value.trim(),
    company: $("editContactCompany").value.trim(),
  };

  if (!payload.email) {
    showNotice("Email address is required.", true);
    return;
  }

  if (button) {
    button.disabled = true;
    button.textContent = "Saving...";
  }

  try {
    await apiRequest(`/contacts/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });

    form.hidden = true;
    form.reset();

    await Promise.all([loadSavedContacts(), loadDashboard()]);
    showNotice("Contact updated successfully.");
  } catch (error) {
    setMessage("editContactMessage", error.message, true);
    showError(error, "Could not update contact");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Save Changes";
    }
  }
}

async function deleteContact(contact, button) {
  const label = contact.email || `Contact ${contact.id}`;

  if (!confirm(`Delete contact "${label}"?`)) return;

  if (button) {
    button.disabled = true;
    button.textContent = "Deleting...";
  }

  try {
    await apiRequest(`/contacts/${encodeURIComponent(contact.id)}`, {
      method: "DELETE",
    });

    selectedContactIds.delete(Number(contact.id));
    scheduleSelectedContactIds.delete(Number(contact.id));

    await Promise.all([loadSavedContacts(), loadDashboard()]);
    showNotice(`Contact "${label}" deleted.`);
  } catch (error) {
    showError(error, "Could not delete contact");

    if (button) {
      button.disabled = false;
      button.textContent = "Delete";
    }
  }
}

// -------------------- CSV IMPORT --------------------

async function importContacts(event) {
  event.preventDefault();

  const file = $("csvFile")?.files?.[0];

  if (!file) {
    showNotice("Choose a CSV file first.", true);
    return;
  }

  const button = $("contactForm")?.querySelector('button[type="submit"]');
  const formData = new FormData();

  formData.append("file", file);

  if (button) {
    button.disabled = true;
    button.textContent = "Importing...";
  }

  try {
    const result = await apiRequest("/contacts/import", {
      method: "POST",
      body: formData,
    });

    setMessage(
      "contactMessage",
      [
        result?.message || "CSV import completed.",
        `Total rows: ${result?.total_rows ?? 0}`,
        `Imported: ${result?.imported_count ?? result?.imported ?? 0}`,
        `Duplicates skipped: ${result?.duplicate_count ?? result?.duplicates_skipped ?? 0}`,
        `Invalid rows: ${result?.invalid_count ?? result?.invalid_rows ?? 0}`,
      ].join("\n")
    );

    const errors = Array.isArray(result?.errors) ? result.errors : [];

    if (errors.length > 0) {
      const escapeCsv = (value) => {
        const text = String(value ?? "");
        return `"${text.replace(/"/g, '""')}"`;
      };

      const rows = [
        ["Row", "Email", "Reason"],
        ...errors.map((error) => [
          error.row,
          error.email,
          error.reason,
        ]),
      ];

      const csvContent =
        "\uFEFF" +
        rows.map((row) => row.map(escapeCsv).join(",")).join("\r\n");

      const blob = new Blob([csvContent], {
        type: "text/csv;charset=utf-8;",
      });

      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");

      link.href = url;
      link.download = "import_errors.csv";

      document.body.appendChild(link);
      link.click();
      link.remove();

      URL.revokeObjectURL(url);

      showNotice("Import completed. Error report downloaded.");
    } else {
      showNotice("CSV import completed. No errors found.");
    }

    $("contactForm").reset();

    await Promise.all([loadSavedContacts(), loadDashboard()]);
  } catch (error) {
    setMessage("contactMessage", error.message, true);
    showError(error, "Could not import CSV");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Import CSV";
    }
  }
}

// -------------------- TEMPLATES --------------------

async function loadTemplates() {
  const result = await apiRequest("/templates");

  templatesCache = getList(result, "templates");

  const select = $("campaignTemplate");

  if (select) {
    select.replaceChildren(new Option("Select a template", ""));

    templatesCache.forEach((template) => {
      select.add(
        new Option(
          template.name || `Template ${template.id}`,
          String(template.id)
        )
      );
    });
  }

  const container = $("templateList");
  if (!container) return;

  container.replaceChildren();

  if (!templatesCache.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No templates saved yet.";
    container.appendChild(empty);
    return;
  }

  templatesCache.forEach((template) => {
    const card = document.createElement("article");
    card.className = "record-card";

    const title = document.createElement("h4");
    title.textContent = template.name || `Template ${template.id}`;

    const subjectText =
      template.subject || template.email_subject || "";

    const bodyText =
      template.body || template.email_body || "";

    const subject = document.createElement("p");
    subject.textContent = `Subject: ${subjectText}`;

    const body = document.createElement("p");
    body.textContent = bodyText;

    const actions = document.createElement("div");
    actions.className = "record-actions";

    const previewButton = document.createElement("button");
    previewButton.type = "button";
    previewButton.className = "button small secondary";
    previewButton.textContent = "Preview";

    previewButton.addEventListener("click", () => {
      if (!contactsCache.length) {
        showNotice("Add or import a contact before previewing.", true);
        return;
      }

      const contactList = contactsCache
        .map((contact, index) => {
          const name =
            [contact.first_name, contact.last_name]
              .filter(Boolean)
              .join(" ") ||
            contact.name ||
            "Unnamed contact";

          return `${index + 1}. ${name} (${contact.email || "No email"})`;
        })
        .join("\n");

      const choice = prompt(
        `Choose a contact for preview:\n\n${contactList}\n\nEnter the contact number:`
      );

      if (choice === null) return;

      const index = Number(choice) - 1;

      if (
        !Number.isInteger(index) ||
        index < 0 ||
        index >= contactsCache.length
      ) {
        showNotice("Invalid contact selection.", true);
        return;
      }

      const contact = contactsCache[index];

      const email = String(contact.email || "").trim();
      const firstName = String(contact.first_name || "").trim();
      const lastName = String(contact.last_name || "").trim();

      const fullName =
        [firstName, lastName].filter(Boolean).join(" ") ||
        String(contact.name || "").trim();

      const nameParts = fullName.split(/\s+/).filter(Boolean);

      const values = {
        first_name: firstName || nameParts[0] || "",
        last_name: lastName || nameParts.slice(1).join(" "),
        name: fullName,
        email,
        company: String(contact.company || "").trim(),
      };

      const personalize = (text) =>
        String(text || "").replace(
          /\{\{\s*(first_name|last_name|name|email|company)\s*\}\}/gi,
          (_, key) => values[key.toLowerCase()] ?? ""
        );

      const overlay = document.createElement("div");

      Object.assign(overlay.style, {
        position: "fixed",
        inset: "0",
        zIndex: "99999",
        background: "rgba(0,0,0,0.65)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      });

      const modal = document.createElement("div");

      Object.assign(modal.style, {
        background: "#fff",
        color: "#222",
        padding: "20px",
        borderRadius: "12px",
        width: "100%",
        maxWidth: "560px",
        maxHeight: "90vh",
        overflowY: "auto",
        boxSizing: "border-box",
      });

      const heading = document.createElement("h2");
      heading.textContent =
        `Edit Preview — ${template.name || "Untitled"}`;

      const recipient = document.createElement("p");
      recipient.textContent = `To: ${contact.email || "No email"}`;

      const subjectLabel = document.createElement("label");
      subjectLabel.textContent = "Subject";
      subjectLabel.style.display = "block";
      subjectLabel.style.marginTop = "12px";

      const subjectInput = document.createElement("input");
      subjectInput.type = "text";
      subjectInput.value = personalize(subjectText);
      subjectInput.style.width = "100%";
      subjectInput.style.boxSizing = "border-box";
      subjectInput.style.padding = "10px";

      const bodyLabel = document.createElement("label");
      bodyLabel.textContent = "Email Body";
      bodyLabel.style.display = "block";
      bodyLabel.style.marginTop = "12px";

      const bodyInput = document.createElement("textarea");
      bodyInput.value = personalize(bodyText);
      bodyInput.rows = 12;
      bodyInput.style.width = "100%";
      bodyInput.style.boxSizing = "border-box";
      bodyInput.style.padding = "10px";
      bodyInput.style.resize = "vertical";

      const buttons = document.createElement("div");

      Object.assign(buttons.style, {
        display: "flex",
        gap: "8px",
        marginTop: "16px",
        flexWrap: "wrap",
      });

      const saveButton = document.createElement("button");
      saveButton.type = "button";
      saveButton.className = "button";
      saveButton.textContent = "Save Changes";

      const cancelButton = document.createElement("button");
      cancelButton.type = "button";
      cancelButton.className = "button secondary";
      cancelButton.textContent = "Cancel";

      const closePreview = () => overlay.remove();

      cancelButton.addEventListener("click", closePreview);

      saveButton.addEventListener("click", async () => {
        const updatedSubject = subjectInput.value.trim();
        const updatedBody = bodyInput.value;

        if (!updatedSubject || !updatedBody.trim()) {
          showNotice("Subject and body cannot be empty.", true);
          return;
        }

        if (
          !confirm(
            "Save these changes to the original template?\n\n" +
            "This updates the saved template for future use."
          )
        ) {
          return;
        }

        saveButton.disabled = true;
        saveButton.textContent = "Saving...";

        try {
          await apiRequest(
            `/templates/${encodeURIComponent(template.id)}`,
            {
              method: "PUT",
              body: JSON.stringify({
                name: template.name,
                subject: updatedSubject,
                body: updatedBody,
              }),
            }
          );

          closePreview();

          await Promise.all([loadTemplates(), loadDashboard()]);
          showNotice("Template updated successfully.");
        } catch (error) {
          showError(error, "Could not update template");
          saveButton.disabled = false;
          saveButton.textContent = "Save Changes";
        }
      });

      buttons.append(saveButton, cancelButton);

      modal.append(
        heading,
        recipient,
        subjectLabel,
        subjectInput,
        bodyLabel,
        bodyInput,
        buttons
      );

      overlay.appendChild(modal);

      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) closePreview();
      });

      document.body.appendChild(overlay);
      subjectInput.focus();
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "button small danger";
    deleteButton.textContent = "Delete";

    deleteButton.addEventListener("click", async () => {
      const label = template.name || `Template ${template.id}`;

      if (!confirm(`Delete template "${label}"?`)) return;

      deleteButton.disabled = true;
      deleteButton.textContent = "Deleting...";

      try {
        await apiRequest(
          `/templates/${encodeURIComponent(template.id)}`,
          { method: "DELETE" }
        );

        await Promise.all([loadTemplates(), loadDashboard()]);
        showNotice(`Template "${label}" deleted.`);
      } catch (error) {
        showError(error, "Could not delete template");
        deleteButton.disabled = false;
        deleteButton.textContent = "Delete";
      }
    });

    actions.append(previewButton, deleteButton);
    card.append(title, subject, body, actions);
    container.appendChild(card);
  });
}

async function saveTemplate(event) {
  event.preventDefault();

  const button = $("templateForm")?.querySelector('button[type="submit"]');

  const payload = {
    name: $("templateName").value.trim(),
    subject: $("emailSubject").value.trim(),
    body: $("emailBody").value,
  };

  if (!payload.name || !payload.subject || !payload.body.trim()) {
    showNotice("Enter a template name, subject, and email body.", true);
    return;
  }

  if (button) {
    button.disabled = true;
    button.textContent = "Saving...";
  }

  try {
    await apiRequest("/templates", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    $("templateForm").reset();
    setMessage("templateMessage", "Template saved successfully.");

    await Promise.all([loadTemplates(), loadDashboard()]);
    showNotice("Template saved successfully.");
  } catch (error) {
    setMessage("templateMessage", error.message, true);
    showError(error, "Could not save template");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Save Template";
    }
  }
}

// -------------------- CAMPAIGN SCHEDULING --------------------

function getScheduleDateTimePayload() {
  const date = $("campaignScheduleDate")?.value || "";
  const time = $("campaignScheduleTime")?.value || "";
  const timezone = $("campaignTimezone")?.value || "Asia/Kolkata";

  if (!date && !time) {
    return {
      scheduled_at: null,
      timezone,
    };
  }

  if (!date || !time) {
    throw new Error(
      "Choose both a schedule date and a schedule time, or leave both empty to create a draft."
    );
  }

  // This UI supports UTC and India Standard Time.
  const offset = timezone === "UTC" ? "Z" : "+05:30";
  const parsed = new Date(`${date}T${time}:00${offset}`);

  if (Number.isNaN(parsed.getTime())) {
    throw new Error("The schedule date or time is invalid.");
  }

  if (parsed.getTime() <= Date.now()) {
    throw new Error("Choose a future date and time for scheduling.");
  }

  return {
    scheduled_at: parsed.toISOString(),
    timezone,
  };
}

async function createCampaign(event) {
  event.preventDefault();

  const form = $("campaignForm");
  const button = form?.querySelector('button[type="submit"]');

  let schedule;

  try {
    schedule = getScheduleDateTimePayload();
  } catch (error) {
    setMessage("campaignMessage", error.message, true);
    showNotice(error.message, true);
    return;
  }

  const contactIds = [...scheduleSelectedContactIds];

  const payload = {
    name: $("campaignName").value.trim(),
    template_id: Number($("campaignTemplate").value),
    contact_ids: contactIds,
    timezone: schedule.timezone,
  };

  if (!payload.name || !payload.template_id) {
    showNotice("Enter a campaign name and select a template.", true);
    return;
  }

  if (!contactIds.length) {
    const message = "Select at least one recipient for this campaign.";
    showNotice(message, true);
    setMessage("campaignMessage", message, true);
    return;
  }

  if (schedule.scheduled_at) {
    payload.scheduled_at = schedule.scheduled_at;
  }

  if (button) {
    button.disabled = true;
    button.textContent = schedule.scheduled_at
      ? "Scheduling..."
      : "Creating...";
  }

  try {
    const result = await apiRequest("/campaigns", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    form.reset();

    // Restore India as the default timezone after resetting the form.
    if ($("campaignTimezone")) {
      $("campaignTimezone").value = "Asia/Kolkata";
    }

    scheduleSelectedContactIds.clear();

    renderScheduleRecipientOptions();
    updateScheduleSelectedContactCount();

    const scheduled = Boolean(schedule.scheduled_at);

    const successMessage =
      result?.message ||
      (scheduled
        ? "Campaign scheduled successfully."
        : "Draft campaign created successfully.");

    setMessage("campaignMessage", successMessage);

    await Promise.all([loadCampaigns(), loadDashboard()]);

    showNotice(
      scheduled
        ? "Campaign scheduled. Check its saved status and scheduled time."
        : "Draft campaign created successfully."
    );
  } catch (error) {
    setMessage("campaignMessage", error.message, true);
    showError(error, "Could not create campaign");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Create / Schedule Campaign";
    }
  }
}

// -------------------- LOAD CAMPAIGNS --------------------

async function loadCampaigns() {
  const result = await apiRequest("/campaigns");

  campaignsCache = getList(result, "campaigns");

  populateCampaignSelect("preflightCampaign", false);
  populateCampaignSelect("recipientCampaign", true);
  populateCampaignSelect("reportCampaign", false);

  renderCampaignList();
}

function populateCampaignSelect(id, draftsOnly) {
  const select = $(id);
  if (!select) return;

  const previousValue = select.value;

  const options = draftsOnly
    ? campaignsCache.filter((campaign) => campaign.status === "draft")
    : campaignsCache;

  select.replaceChildren(
    new Option(
      draftsOnly ? "Select a draft campaign" : "Select a campaign",
      ""
    )
  );

  options.forEach((campaign) => {
    select.add(
      new Option(
        `${campaign.name || `Campaign ${campaign.id}`} (${campaign.status || "unknown"})`,
        String(campaign.id)
      )
    );
  });

  if (options.some((campaign) => String(campaign.id) === previousValue)) {
    select.value = previousValue;
  }
}

function renderCampaignList() {
  const container = $("campaignList");
  if (!container) return;

  container.replaceChildren();

  if (!campaignsCache.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No campaigns created yet.";
    container.appendChild(empty);
    return;
  }

  campaignsCache.forEach((campaign) => {
    const card = document.createElement("article");
    card.className = "record-card";

    const title = document.createElement("h4");
    title.textContent = campaign.name || `Campaign ${campaign.id}`;

    const status = document.createElement("p");
    status.textContent =
      `Status: ${campaign.status || "unknown"} · ID: ${campaign.id}`;

    const details = document.createElement("p");

    if (campaign.scheduled_at) {
      const scheduledDate = new Date(campaign.scheduled_at);

      details.textContent = Number.isNaN(scheduledDate.getTime())
        ? `Scheduled at: ${campaign.scheduled_at}`
        : `Scheduled at: ${scheduledDate.toLocaleString()} (${campaign.timezone || "timezone not specified"})`;
    } else {
      details.textContent = "No scheduled time.";
    }

    const actions = document.createElement("div");
    actions.className = "record-actions";

    const preflightButton = document.createElement("button");
    preflightButton.type = "button";
    preflightButton.className = "button small secondary";
    preflightButton.textContent = "Preflight";

    preflightButton.addEventListener("click", () => {
      if ($("preflightCampaign")) {
        $("preflightCampaign").value = String(campaign.id);
      }

      runPreflight();
    });

    const reportButton = document.createElement("button");
    reportButton.type = "button";
    reportButton.className = "button small secondary";
    reportButton.textContent = "View Report";

    reportButton.addEventListener("click", () => {
      if ($("reportCampaign")) {
        $("reportCampaign").value = String(campaign.id);
      }

      loadCampaignReport(campaign.id);
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "button small danger";
    deleteButton.textContent = "Delete Campaign";

    deleteButton.addEventListener("click", () =>
      deleteCampaign(campaign, deleteButton)
    );

    actions.append(preflightButton, reportButton, deleteButton);
    card.append(title, status, details, actions);
    container.appendChild(card);
  });
}

async function deleteCampaign(campaign, button) {
  const label = campaign.name || `Campaign ${campaign.id}`;

  if (campaign.status === "sending") {
    showNotice("This campaign is sending. Wait until it finishes.", true);
    return;
  }

  if (
    !confirm(
      `Delete campaign "${label}"?\n\nThis may also delete its associated recipient and report records.`
    )
  ) {
    return;
  }

  if (button) {
    button.disabled = true;
    button.textContent = "Deleting...";
  }

  try {
    await apiRequest(`/campaigns/${encodeURIComponent(campaign.id)}`, {
      method: "DELETE",
    });

    await Promise.all([loadCampaigns(), loadDashboard()]);

    if ($("reportCampaign")?.value === String(campaign.id)) {
      $("reportCampaign").value = "";

      if ($("campaignReportResult")) {
        $("campaignReportResult").textContent =
          "Campaign deleted. Select another campaign to view its report.";
      }
    }

    showNotice(`Campaign "${label}" deleted.`);
  } catch (error) {
    showError(error, "Could not delete campaign");

    if (button) {
      button.disabled = false;
      button.textContent = "Delete Campaign";
    }
  }
}

// -------------------- CAMPAIGN PREFLIGHT --------------------

async function runPreflight() {
  const campaignId = $("preflightCampaign")?.value;

  if (!campaignId) {
    showNotice("Select a campaign for preflight.", true);
    return;
  }

  const button = $("preflightBtn");

  if (button) {
    button.disabled = true;
    button.textContent = "Checking...";
  }

  try {
    const result = await apiRequest(
      `/campaigns/${encodeURIComponent(campaignId)}/preflight`,
      { method: "POST" }
    );

    const container = $("preflightResult");

    if (container) {
      container.textContent = JSON.stringify(result, null, 2);
    }
  } catch (error) {
    if ($("preflightResult")) {
      $("preflightResult").textContent = error.message;
    }

    showError(error, "Preflight failed");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Check Campaign";
    }
  }
}

// -------------------- IMMEDIATE-SEND RECIPIENTS --------------------

function renderRecipientOptions() {
  const container = $("recipientList");
  if (!container) return;

  container.replaceChildren();

  const eligibleContacts = contactsCache.filter(
    (contact) => !contact.is_unsubscribed
  );

  if (!eligibleContacts.length) {
    container.textContent =
      "No eligible contacts available. Add contacts first.";

    updateSelectedContactCount();
    return;
  }

  eligibleContacts.forEach((contact) => {
    const label = document.createElement("label");

    label.style.display = "flex";
    label.style.alignItems = "center";
    label.style.gap = "9px";
    label.style.padding = "8px 0";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedContactIds.has(Number(contact.id));

    checkbox.addEventListener("change", () => {
      const id = Number(contact.id);

      if (checkbox.checked) {
        selectedContactIds.add(id);
      } else {
        selectedContactIds.delete(id);
      }

      updateSelectedContactCount();
    });

    const text = document.createElement("span");
    text.textContent = contact.email || `Contact ${contact.id}`;

    label.append(checkbox, text);
    container.appendChild(label);
  });

  updateSelectedContactCount();
}

function updateSelectedContactCount() {
  const element = $("selectedContactCount");

  if (element) {
    element.textContent =
      `${selectedContactIds.size} contact(s) selected.`;
  }
}

// -------------------- SCHEDULED CAMPAIGN RECIPIENTS --------------------

function renderScheduleRecipientOptions() {
  const container = $("scheduleRecipientList");
  if (!container) return;

  container.replaceChildren();

  const eligibleContacts = contactsCache.filter(
    (contact) => !contact.is_unsubscribed
  );

  if (!eligibleContacts.length) {
    container.textContent =
      "No eligible contacts available. Add contacts first; unsubscribed contacts cannot be selected.";

    updateScheduleSelectedContactCount();
    return;
  }

  eligibleContacts.forEach((contact) => {
    const label = document.createElement("label");

    label.style.display = "flex";
    label.style.alignItems = "center";
    label.style.gap = "9px";
    label.style.padding = "8px 0";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = scheduleSelectedContactIds.has(Number(contact.id));

    checkbox.setAttribute(
      "aria-label",
      `Select ${contact.email || `contact ${contact.id}`} for campaign`
    );

    checkbox.addEventListener("change", () => {
      const id = Number(contact.id);

      if (checkbox.checked) {
        scheduleSelectedContactIds.add(id);
      } else {
        scheduleSelectedContactIds.delete(id);
      }

      updateScheduleSelectedContactCount();
    });

    const text = document.createElement("span");
    text.textContent = contact.email || `Contact ${contact.id}`;

    label.append(checkbox, text);
    container.appendChild(label);
  });

  updateScheduleSelectedContactCount();
}

function updateScheduleSelectedContactCount() {
  const element = $("scheduleSelectedContactCount");

  if (element) {
    element.textContent =
      `${scheduleSelectedContactIds.size} contact(s) selected for this campaign.`;
  }
}

function selectAllScheduleContacts() {
  contactsCache
    .filter((contact) => !contact.is_unsubscribed)
    .forEach((contact) => {
      scheduleSelectedContactIds.add(Number(contact.id));
    });

  renderScheduleRecipientOptions();
}

function clearScheduleContacts() {
  scheduleSelectedContactIds.clear();
  renderScheduleRecipientOptions();
}

// -------------------- SEND CAMPAIGN NOW --------------------

async function sendSelectedCampaign() {
  const campaignId = $("recipientCampaign")?.value;

  const campaign = campaignsCache.find(
    (item) => String(item.id) === String(campaignId)
  );

  if (!campaign || campaign.status !== "draft") {
    showNotice("Select a draft campaign before sending.", true);
    return;
  }

  const contactIds = [...selectedContactIds];

  if (!contactIds.length) {
    showNotice("Select at least one recipient.", true);
    return;
  }

  if (
    !confirm(
      `Send campaign "${campaign.name}" to ${contactIds.length} selected contact(s)?\n\nThis sends real emails.`
    )
  ) {
    return;
  }

  const button = $("sendSelectedCampaignBtn");

  if (button) {
    button.disabled = true;
    button.textContent = "Sending...";
  }

  try {
    const result = await apiRequest(
      `/campaigns/${encodeURIComponent(campaignId)}/send`,
      {
        method: "POST",
        body: JSON.stringify({ contact_ids: contactIds }),
      }
    );

    showNotice(result?.message || "Campaign send request completed.");

    await Promise.all([loadCampaigns(), loadDashboard()]);
    await loadCampaignReport(campaignId);
  } catch (error) {
    showError(error, "Campaign send failed");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Send to Selected Contacts";
    }
  }
}

// -------------------- CAMPAIGN REPORT --------------------

async function loadCampaignReport(campaignId) {
  if (!campaignId) {
    showNotice("Select a campaign to view its report.", true);
    return;
  }

  const container = $("campaignReportResult");

  if (container) {
    container.textContent = "Loading report...";
  }

  try {
    const result = await apiRequest(
      `/campaigns/${encodeURIComponent(campaignId)}/report`
    );

    if (container) {
      container.textContent = JSON.stringify(result, null, 2);
    }
  } catch (error) {
    if (container) {
      container.textContent = error.message;
    }

    showError(error, "Could not load campaign report");
  }
}

// -------------------- RESET APPLICATION DATA --------------------

async function resetAllData() {
  if (
    !confirm(
      "WARNING: Reset ALL MailPilot application data?\n\n" +
      "Contacts, templates, campaigns, and tracking records may be permanently deleted."
    )
  ) {
    return;
  }

  if (prompt("Type RESET to confirm:") !== "RESET") {
    showNotice("Reset cancelled.", true);
    return;
  }

  const button = $("resetDataBtn");

  if (button) {
    button.disabled = true;
    button.textContent = "Resetting...";
  }

  try {
    await apiRequest(RESET_ENDPOINT, { method: "POST" });

    selectedContactIds.clear();
    scheduleSelectedContactIds.clear();

    await Promise.all([
      loadSavedContacts(),
      loadTemplates(),
      loadCampaigns(),
      loadDashboard(),
    ]);

    showNotice("Application data reset completed.");
  } catch (error) {
    showError(
      error,
      `Reset failed. Confirm that ${RESET_ENDPOINT} exists and is protected`
    );
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Reset All Data";
    }
  }
}

// -------------------- INITIALIZATION --------------------

function initialize() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      showSection(tab.dataset.section);
    });
  });

  document.querySelectorAll("[data-go]").forEach((button) => {
    button.addEventListener("click", () => {
      showSection(button.dataset.go);
    });
  });

  $("connectGmailBtn")?.addEventListener("click", openAuthPage);

  $("addContactForm")?.addEventListener("submit", addContact);
  $("editContactForm")?.addEventListener("submit", saveEditedContact);

  $("cancelEditContactBtn")?.addEventListener("click", () => {
    const form = $("editContactForm");

    if (form) {
      form.hidden = true;
      form.reset();
    }
  });

  $("contactForm")?.addEventListener("submit", importContacts);

  $("refreshContactsListBtn")?.addEventListener("click", () =>
    loadSavedContacts().catch((error) =>
      showError(error, "Could not refresh contacts")
    )
  );

  $("templateForm")?.addEventListener("submit", saveTemplate);
  $("campaignForm")?.addEventListener("submit", createCampaign);

  // Scheduling recipient controls.
  $("refreshScheduleContactsBtn")?.addEventListener("click", () =>
    loadContacts().catch((error) =>
      showError(error, "Could not refresh schedule recipients")
    )
  );

  $("selectAllScheduleContactsBtn")?.addEventListener(
    "click",
    selectAllScheduleContacts
  );

  $("clearScheduleContactsBtn")?.addEventListener(
    "click",
    clearScheduleContacts
  );

  // Preflight.
  $("preflightBtn")?.addEventListener("click", runPreflight);

  // Immediate-send recipient controls.
  $("refreshContactsBtn")?.addEventListener("click", () =>
    loadContacts().catch((error) =>
      showError(error, "Could not refresh recipient contacts")
    )
  );

  $("selectAllContactsBtn")?.addEventListener("click", () => {
    contactsCache
      .filter((contact) => !contact.is_unsubscribed)
      .forEach((contact) => {
        selectedContactIds.add(Number(contact.id));
      });

    renderRecipientOptions();
  });

  $("clearContactsBtn")?.addEventListener("click", () => {
    selectedContactIds.clear();
    renderRecipientOptions();
  });

  $("recipientCampaign")?.addEventListener("change", () => {
    const campaign = campaignsCache.find(
      (item) => String(item.id) === $("recipientCampaign").value
    );

    const button = $("sendSelectedCampaignBtn");

    if (button) {
      button.disabled = !campaign || campaign.status !== "draft";
      button.textContent = campaign
        ? "Send to Selected Contacts"
        : "Select a draft campaign";
    }
  });

  $("sendSelectedCampaignBtn")?.addEventListener(
    "click",
    sendSelectedCampaign
  );

  // Reports.
  $("loadReportBtn")?.addEventListener("click", () =>
    loadCampaignReport($("reportCampaign")?.value)
  );

  $("reportCampaign")?.addEventListener("change", () => {
    if ($("reportCampaign").value) {
      loadCampaignReport($("reportCampaign").value);
    }
  });

  $("refreshCampaignsBtn")?.addEventListener("click", () =>
    loadCampaigns().catch((error) =>
      showError(error, "Could not refresh campaigns")
    )
  );

  $("resetDataBtn")?.addEventListener("click", resetAllData);

  // Initial data loading.
  loadSavedContacts().catch((error) =>
    showError(error, "Could not load contacts")
  );

  loadDashboard().catch((error) =>
    showError(error, "Could not load dashboard")
  );

  loadTemplates().catch((error) =>
    showError(error, "Could not load templates")
  );

  loadCampaigns().catch((error) =>
    showError(error, "Could not load campaigns")
  );

  checkGmailStatus();
}

document.addEventListener("DOMContentLoaded", initialize);