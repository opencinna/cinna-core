// The dashboard reads its own data file over its capability path.
fetch("data.csv")
  .then((r) => r.text())
  .then((text) => {
    const body = document.querySelector("#sales tbody")
    for (const line of text.trim().split("\n").slice(1)) {
      const [region, revenue] = line.split(",")
      const row = document.createElement("tr")
      row.innerHTML = `<td>${region}</td><td>${revenue}</td>`
      body.appendChild(row)
    }
    document.body.dataset.ready = "true"
  })
