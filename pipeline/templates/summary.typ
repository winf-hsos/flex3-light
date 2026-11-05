// One page per language: the summary of a lecture (pipeline/summary.py writes the data).
// Each page picks the largest text size at which it fits on one A4 page and reports it as <fit>.

#let data = json(sys.inputs.at("data", default: "summary-data.json"))
#let blue = rgb("#009ee3")
#let ink = rgb("#1b2733")
#let slate = rgb("#5e6b78")
#let line-color = rgb("#d5dde4")
#let sizes = range(44, 31, step: -1).map(q => q * 0.25pt)    // 11pt down to 8pt

#set document(title: data.pages.first().title)
#set page(paper: "a4", margin: (x: 20mm, top: 17mm, bottom: 22mm),
  footer: context {
    let p = data.pages.at(calc.min(counter(page).get().first(), data.pages.len()) - 1)
    set text(size: 7.5pt, fill: slate)
    line(length: 100%, stroke: 0.5pt + line-color)
    v(-0.4em)
    p.footer
  })
// lines are measured from ascender to descender, so that spacing never lets them touch
#set text(font: ("Segoe UI", "Noto Sans", "DejaVu Sans", "Libertinus Serif"), fill: ink,
  top-edge: "ascender", bottom-edge: "descender")
#set par(leading: 0.3em, spacing: 0.7em)
#set list(indent: 0pt, body-indent: 0.6em, spacing: 0.6em, marker: text(fill: blue, sym.bullet))

#let section(title) = block(above: 1.4em, below: 0.5em, sticky: true,
  text(size: 1.08em, weight: "semibold", fill: blue, title))

#let page-body(p, size) = {
  set text(size: size, lang: p.lang)
  grid(columns: (1fr, auto), align: (left + bottom, right + bottom),
    text(size: 0.95em, weight: "semibold", fill: blue, p.module),
    text(size: 0.95em, fill: slate, p.labels.kind))
  block(above: 0.3em, below: 0.3em, text(size: 2.15em, weight: "bold", hyphenate: false, p.title))
  text(fill: slate, p.meta)
  block(above: 1.2em, below: 1.5em, text(size: 1.12em, p.overview))
  grid(columns: (1.4fr, 1fr), column-gutter: 8mm,
    {
      section(p.labels.points)
      list(..p.points)
    },
    {
      section(p.labels.terms)
      for t in p.terms {
        block(below: 0.6em, breakable: false)[
          #text(weight: "semibold", t.term) \
          #text(fill: slate, t.explanation)
        ]
      }
    })
  if p.notes.len() > 0 {
    section(p.labels.notes)
    list(..p.notes)
  }
  if p.chapters.len() > 0 {
    section(p.labels.chapters)
    let half = calc.ceil(p.chapters.len() / 2)
    let column(items) = grid(columns: (auto, 1fr), column-gutter: 0.9em, row-gutter: 0.35em,
      ..items.map(c => (text(fill: slate, number-width: "tabular", c.time), c.title)).flatten())
    grid(columns: (1fr, 1fr), column-gutter: 8mm,
      column(p.chapters.slice(0, half)), column(p.chapters.slice(half)))
  }
}

#for (i, p) in data.pages.enumerate() {
  if i > 0 { pagebreak() }
  layout(region => {
    let fits(s) = measure(block(width: region.width, page-body(p, s))).height <= region.height
    let chosen = sizes.find(fits)
    [#metadata((page: i + 1, size: repr(if chosen == none { sizes.last() } else { chosen }),
                fits: chosen != none)) <fit>]
    page-body(p, if chosen == none { sizes.last() } else { chosen })
  })
}
