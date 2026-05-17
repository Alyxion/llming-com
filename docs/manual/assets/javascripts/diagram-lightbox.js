(function () {
  const DIAGRAM_SELECTOR = [
    ".mermaid",
    ".md-content img[src$='.svg']",
    ".md-content img[src*='.svg?']",
    ".md-content img[src*='.excalidraw']",
  ].join(",");

  function ensureLightbox() {
    let lightbox = document.querySelector(".llming-diagram-lightbox");
    if (lightbox) return lightbox;

    lightbox = document.createElement("div");
    lightbox.className = "llming-diagram-lightbox";
    lightbox.setAttribute("aria-hidden", "true");
    lightbox.innerHTML = [
      '<div class="llming-diagram-lightbox__toolbar">',
      '<button class="llming-diagram-lightbox__button" data-llming-zoom="out" type="button" title="Zoom out">-</button>',
      '<button class="llming-diagram-lightbox__button" data-llming-zoom="reset" type="button" title="Reset zoom">100%</button>',
      '<button class="llming-diagram-lightbox__button" data-llming-zoom="in" type="button" title="Zoom in">+</button>',
      '<button class="llming-diagram-lightbox__button" data-llming-close type="button" title="Close">Esc</button>',
      "</div>",
      '<div class="llming-diagram-lightbox__canvas">',
      '<div class="llming-diagram-lightbox__content"></div>',
      "</div>",
    ].join("");
    document.body.appendChild(lightbox);
    return lightbox;
  }

  function setScale(lightbox, scale) {
    lightbox.dataset.scale = String(scale);
    const content = lightbox.querySelector(".llming-diagram-lightbox__content");
    if (content) content.style.transform = "scale(" + scale + ")";
  }

  function closeLightbox() {
    const lightbox = document.querySelector(".llming-diagram-lightbox");
    if (!lightbox) return;
    lightbox.setAttribute("aria-hidden", "true");
    const content = lightbox.querySelector(".llming-diagram-lightbox__content");
    if (content) content.replaceChildren();
    document.documentElement.style.overflow = "";
  }

  function cloneDiagram(source) {
    const svg = source.matches("svg") ? source : source.querySelector("svg");
    if (svg) {
      const clone = svg.cloneNode(true);
      clone.removeAttribute("id");
      clone.removeAttribute("style");
      return clone;
    }

    if (source.matches("img")) {
      const clone = source.cloneNode(true);
      clone.removeAttribute("id");
      return clone;
    }

    return null;
  }

  function openLightbox(source) {
    const clone = cloneDiagram(source);
    if (!clone) return;

    const lightbox = ensureLightbox();
    const content = lightbox.querySelector(".llming-diagram-lightbox__content");
    if (!content) return;

    content.replaceChildren(clone);
    setScale(lightbox, 1);
    lightbox.setAttribute("aria-hidden", "false");
    document.documentElement.style.overflow = "hidden";
  }

  function attachHandlers(root) {
    root.querySelectorAll(DIAGRAM_SELECTOR).forEach(function (block) {
      if (block.dataset.llmingDiagramLightbox === "1") return;
      if (block.matches(".mermaid") && !block.querySelector("svg")) return;

      block.dataset.llmingDiagramLightbox = "1";
      block.setAttribute("role", "button");
      block.setAttribute("tabindex", "0");
      block.setAttribute("aria-label", "Open diagram in larger view");
      block.addEventListener("click", function () {
        openLightbox(block);
      });
      block.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openLightbox(block);
        }
      });
    });
  }

  document.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const close = target.closest("[data-llming-close]");
    if (close) {
      closeLightbox();
      return;
    }

    const zoom = target.closest("[data-llming-zoom]");
    if (!zoom) return;

    const lightbox = ensureLightbox();
    const current = Number(lightbox.dataset.scale || "1");
    const action = zoom.getAttribute("data-llming-zoom");
    if (action === "in") setScale(lightbox, Math.min(4, current + 0.25));
    if (action === "out") setScale(lightbox, Math.max(0.5, current - 0.25));
    if (action === "reset") setScale(lightbox, 1);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeLightbox();
  });

  let observerStarted = false;

  function startObserver() {
    if (observerStarted || !document.body) return;
    observerStarted = true;
    const observer = new MutationObserver(function () {
      attachHandlers(document);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  function initLightbox() {
    attachHandlers(document);
    startObserver();
  }

  if (
    typeof document$ !== "undefined" &&
    document$ &&
    typeof document$.subscribe === "function"
  ) {
    document$.subscribe(initLightbox);
  } else {
    document.addEventListener("DOMContentLoaded", initLightbox);
  }
})();
