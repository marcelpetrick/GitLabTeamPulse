// About dialog: version, author, license and runtime dependencies (from the server's metadata).

import { api } from "./api.js";
import { h, safeLink } from "./dom.js";
import { toast } from "./ui.js";

export class AboutDialog {
  constructor() {
    this.dialog = document.getElementById("about");
    this.body = document.getElementById("about-body");
    this.info = null;
    document.getElementById("open-about").addEventListener("click", () => this.open());
    document.getElementById("close-about").addEventListener("click", () => this.dialog.close());
    // A click on the backdrop (outside the dialog box) closes it, like Escape does.
    this.dialog.addEventListener("click", (event) => { if (event.target === this.dialog) this.dialog.close(); });
  }

  async open() {
    if (!this.info) {
      try {
        this.info = await api.about();
      } catch (error) {
        toast(`Could not load the about information: ${error.message}`, "error");
        return;
      }
    }
    this.render(this.info);
    this.dialog.showModal();
  }

  render(info) {
    const repository = safeLink(info.repository, info.repository.replace(/^https:\/\//, ""));
    this.body.replaceChildren(
      h("div", { class: "about-brand" },
        h("strong", { class: "about-name" }, info.name),
        h("span", { class: "badge accent", id: "about-version" }, `v${info.version}`)),
      h("p", { class: "muted" }, info.description),
      h("dl", { class: "about-facts" },
        h("dt", {}, "Author"), h("dd", {}, info.author, " · ", safeLink(`mailto:${info.email}`, info.email)),
        h("dt", {}, "License"), h("dd", {}, info.license),
        h("dt", {}, "Source"), h("dd", {}, repository),
        h("dt", {}, "Python"), h("dd", {}, info.python)),
      h("h3", {}, "Runtime dependencies"),
      h("table", { class: "about-deps" },
        h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Package"), h("th", { scope: "col" }, "Version"), h("th", { scope: "col" }, "License"))),
        h("tbody", {}, info.dependencies.map((dep) => h("tr", {},
          h("td", {}, dep.name), h("td", { class: "mono" }, dep.version || "—"), h("td", {}, dep.license))))),
      h("p", { class: "muted about-note" }, info.note));
  }
}
