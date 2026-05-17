(function () {
  let attempts = 0;

  function colorScheme() {
    return document.body.getAttribute("data-md-color-scheme") === "slate"
      ? "dark"
      : "default";
  }

  function renderMermaid() {
    attempts += 1;
    if (!window.mermaid) {
      if (attempts < 40) window.setTimeout(renderMermaid, 100);
      return;
    }

    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: colorScheme(),
    });

    document.querySelectorAll("pre.mermaid").forEach(function (block) {
      if (block.dataset.llmingMermaidPrepared === "1") return;
      const code = block.querySelector("code");
      if (!code) return;
      const diagram = document.createElement("div");
      diagram.className = block.className;
      diagram.dataset.llmingMermaidPrepared = "1";
      diagram.textContent = code.textContent;
      block.replaceWith(diagram);
    });

    window.mermaid.run({
      nodes: Array.from(
        document.querySelectorAll(".mermaid:not([data-processed='true'])")
      ),
    }).catch(function (error) {
      console.error("Mermaid render failed", error);
    });
  }

  function initMermaid() {
    attempts = 0;
    renderMermaid();
  }

  if (
    typeof document$ !== "undefined" &&
    document$ &&
    typeof document$.subscribe === "function"
  ) {
    document$.subscribe(initMermaid);
  } else {
    document.addEventListener("DOMContentLoaded", initMermaid);
  }
})();
