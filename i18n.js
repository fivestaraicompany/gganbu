const DEFAULT_LANGUAGE = "en";

const SUPPORTED_LANGUAGES = [
  "uk", "ln", "yo", "mk", "ts", "so", "ar", "bg", "da", "de", "el", "en",
  "es", "fa", "fi", "fr", "he", "hi", "hu", "id", "it", "ja", "ko", "ml",
  "mn", "nl", "pl", "pt-BR", "pt-PT", "ru", "sl", "sv", "th", "tr", "vi",
  "zh-HK", "zh-Hans", "zh-Hant", "ms", "sk", "cs", "hr", "lt", "ta", "te",
  "mr", "bn", "gu", "pa", "or", "am", "kn", "kk", "ro", "ur", "pcm",
  "es-US", "af", "sw", "zu", "fil", "my", "ne", "si", "et", "lv", "az",
  "sr", "ka", "sq", "ti", "om", "ig", "ha", "rw", "st", "bs", "hy", "be",
  "es-MX", "fr-CA", "ca", "nb"
];

const SUPPORTED_BY_NORMALIZED_CODE = new Map(
  SUPPORTED_LANGUAGES.map(code => [code.toLowerCase(), code])
);

const LANGUAGE_ALIASES = new Map([
  ["in", "id"],
  ["iw", "he"],
  ["no", "nb"],
  ["pt", "pt-PT"],
  ["tl", "fil"],
  ["zh", "zh-Hans"],
  ["zh-cn", "zh-Hans"],
  ["zh-sg", "zh-Hans"],
  ["zh-tw", "zh-Hant"],
  ["zh-mo", "zh-Hant"]
]);

const RTL_LANGUAGES = new Set(["ar", "fa", "he", "ur"]);
const STORAGE_KEY = "gganbu_lang";
let applySequence = 0;

function normalizeLanguageCode(code) {
  return String(code || "").trim().replaceAll("_", "-").toLowerCase();
}

function resolveLanguage(code) {
  const normalized = normalizeLanguageCode(code);
  if (!normalized) return null;

  const aliased = LANGUAGE_ALIASES.get(normalized);
  if (aliased) return aliased;

  const exact = SUPPORTED_BY_NORMALIZED_CODE.get(normalized);
  if (exact) return exact;

  if (normalized.startsWith("zh-hans")) return "zh-Hans";
  if (normalized.startsWith("zh-hant-hk")) return "zh-HK";
  if (normalized.startsWith("zh-hant")) return "zh-Hant";

  const base = normalized.split("-")[0];
  return LANGUAGE_ALIASES.get(base)
    || SUPPORTED_BY_NORMALIZED_CODE.get(base)
    || null;
}

function readSavedLanguage() {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function saveLanguage(language) {
  try {
    localStorage.setItem(STORAGE_KEY, language);
  } catch {
    // The page still works when storage is disabled.
  }
}

function detectLanguage() {
  const saved = resolveLanguage(readSavedLanguage());
  if (saved) return saved;

  const browserLanguages = navigator.languages?.length
    ? navigator.languages
    : [navigator.language];

  for (const candidate of browserLanguages) {
    const resolved = resolveLanguage(candidate);
    if (resolved) return resolved;
  }
  return DEFAULT_LANGUAGE;
}

async function loadLanguageFile(language) {
  const url = new URL(`locales/${language}.json`, import.meta.url);
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Unable to load ${url.pathname}: HTTP ${response.status}`);
  }

  const value = await response.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${url.pathname} must contain a JSON object.`);
  }
  return value;
}

function pageStrings(locale, page) {
  const value = locale?.[page];
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function updateDocumentLanguage(language) {
  document.documentElement.lang = language;
  document.documentElement.dir = RTL_LANGUAGES.has(language.split("-")[0]) ? "rtl" : "ltr";
}

function applyStrings(strings) {
  document.querySelectorAll("[data-i18n]").forEach(element => {
    const key = element.dataset.i18n;
    const value = strings[key];
    if (typeof value !== "string") return;

    const attribute = element.dataset.i18nAttr;
    if (attribute) {
      element.setAttribute(attribute, value);
    } else if (key === "meta.title") {
      document.title = value;
    } else if (element.hasAttribute("data-i18n-html")) {
      element.innerHTML = value;
    } else {
      element.textContent = value;
    }
  });
}

async function applyLanguage(language) {
  const resolved = resolveLanguage(language) || DEFAULT_LANGUAGE;
  const sequence = ++applySequence;

  try {
    const english = await loadLanguageFile(DEFAULT_LANGUAGE);
    const selected = resolved === DEFAULT_LANGUAGE
      ? english
      : await loadLanguageFile(resolved);
    if (sequence !== applySequence) return;

    const page = document.body.dataset.i18nPage;
    const strings = {
      ...pageStrings(english, page),
      ...pageStrings(selected, page)
    };

    applyStrings(strings);
    updateDocumentLanguage(resolved);
    saveLanguage(resolved);

    const selector = document.getElementById("langSelect");
    if (selector) selector.value = resolved;
  } catch (error) {
    console.error("Failed to apply page translation.", error);
  }
}

function initializeLanguageSelector() {
  const selector = document.getElementById("langSelect");
  if (!selector) return;

  for (const code of SUPPORTED_LANGUAGES) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = code;
    selector.appendChild(option);
  }

  selector.addEventListener("change", event => {
    applyLanguage(event.target.value);
  });
}

initializeLanguageSelector();
applyLanguage(detectLanguage());
