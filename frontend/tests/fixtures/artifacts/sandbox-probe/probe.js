// core.md §4 security tests 1 and 2. Every "blocked" check must come back
// blocked; every "allowed" check must succeed. The verdict is written as JSON
// into <pre id="result">. The other artifact's capability URL and the app's
// origin arrive in location.hash (top-level) or as runProbe's argument (framed).
;(() => {
  const outcome = async (fn) => {
    try {
      const value = await fn()
      return { ok: true, value: String(value) }
    } catch (error) {
      return {
        ok: false,
        error: String(error?.name ? error.name : error),
      }
    }
  }

  const fetchText = async (url) => {
    const response = await fetch(url)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return (await response.text()).length
  }

  // A network check: blocked only when fetch itself rejects (CSP / network
  // refusal surfaces as a TypeError). Any HTTP response — a 401 or 404
  // included — means the request reached a server, so it is NOT blocked.
  const reach = async (url) => {
    const response = await fetch(url)
    return `HTTP ${response.status}`
  }

  const loadImage = (src) =>
    new Promise((resolve, reject) => {
      const img = new Image()
      img.onload = () => resolve("loaded")
      img.onerror = () => reject(new Error("blocked"))
      img.src = src
      setTimeout(() => reject(new Error("timeout")), 5000)
    })

  const waitForModule = () =>
    new Promise((resolve, reject) => {
      const started = Date.now()
      const tick = () => {
        if (window.__moduleLoaded) resolve("loaded")
        else if (Date.now() - started > 5000) reject(new Error("timeout"))
        else setTimeout(tick, 50)
      }
      tick()
    })

  window.runProbe = async (params) => {
    const otherCap = params.otherCap
    const appOrigin = params.appOrigin
    const blocked = {
      localStorageToken: await outcome(() => {
        const token = window.localStorage.getItem("access_token")
        if (!token) throw new Error("no token")
        return token
      }),
      apiRelative: await outcome(() => reach("/api/v1/users/me")),
      apiAppOrigin: await outcome(() => reach(`${appOrigin}/api/v1/users/me`)),
      otherArtifact: await outcome(() => reach(otherCap)),
      externalImage: await outcome(() =>
        loadImage("https://example.com/probe.png"),
      ),
    }
    // The app's document is only reachable to test when framed: at top level
    // `window.opener` is null (noopener), so the check would pass vacuously.
    if (window.parent !== window) {
      blocked.appDocument = await outcome(() => window.parent.document.title)
    }
    const allowed = {
      ownFetch: await outcome(() => fetchText("data.sqlite")),
      module: await outcome(waitForModule),
      wasm: await outcome(async () => {
        const response = await fetch("tiny.wasm")
        const bytes = await response.arrayBuffer()
        const { instance } = await WebAssembly.instantiate(bytes)
        return typeof instance
      }),
    }
    const verdict = { blocked, allowed }
    document.getElementById("result").textContent = JSON.stringify(verdict)
    return verdict
  }

  if (location.hash.length > 1) {
    const params = JSON.parse(decodeURIComponent(location.hash.slice(1)))
    window.runProbe(params)
  }
})()
