"""
HTML templates for public webapp pages (error pages, loading screens).
"""

ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Web App Not Available</title>
<style>
  body {{ margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #0a0a0b; color: #e4e4e7; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .container {{ text-align: center; max-width: 400px; padding: 2rem; }}
  h2 {{ font-size: 1.25rem; font-weight: 600; margin-bottom: 0.75rem; }}
  p {{ color: #a1a1aa; font-size: 0.875rem; line-height: 1.5; }}
  button {{ margin-top: 1.5rem; padding: 0.5rem 1rem; background: #3b82f6; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 0.875rem; }}
  button:hover {{ background: #2563eb; }}
  .icon {{ font-size: 2.5rem; margin-bottom: 1rem; }}
</style>
</head>
<body>
<div class="container">
  <div class="icon">&#128679;</div>
  <h2>{title}</h2>
  <p>{message}</p>
  <button onclick="location.reload()">Retry</button>
</div>
</body>
</html>"""


LOADING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Loading Agent App</title>
<style>
  body {{ margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #0a0a0b; color: #e4e4e7; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .container {{ text-align: center; max-width: 400px; padding: 2rem; }}
  h2 {{ font-size: 1.25rem; font-weight: 600; margin-bottom: 1.5rem; }}
  .steps {{ text-align: left; margin: 1.5rem 0; }}
  .step {{ display: flex; align-items: center; gap: 0.75rem; margin: 0.75rem 0; font-size: 0.875rem; color: #a1a1aa; }}
  .step.active {{ color: #e4e4e7; }}
  .step.done {{ color: #22c55e; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #3f3f46; flex-shrink: 0; }}
  .step.active .dot {{ background: #3b82f6; animation: pulse 1.5s infinite; }}
  .step.done .dot {{ background: #22c55e; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}
  .progress {{ height: 3px; background: #27272a; border-radius: 2px; overflow: hidden; margin-top: 1.5rem; }}
  .progress-bar {{ height: 100%; background: #3b82f6; width: 33%; transition: width 0.5s; }}
  .error {{ color: #ef4444; margin-top: 1rem; font-size: 0.875rem; }}
  button {{ margin-top: 1rem; padding: 0.5rem 1rem; background: #3b82f6; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 0.875rem; }}
  button:hover {{ background: #2563eb; }}
</style>
</head>
<body>
<div class="container">
  <h2>Loading Agent App</h2>
  <div class="steps">
    <div class="step active" id="step1"><span class="dot"></span> Waking up the agent...</div>
    <div class="step" id="step2"><span class="dot"></span> Loading the app...</div>
    <div class="step" id="step3"><span class="dot"></span> Ready</div>
  </div>
  <div class="progress"><div class="progress-bar" id="progress"></div></div>
  <div id="error-msg" class="error" style="display:none"></div>
  <button id="retry-btn" style="display:none" onclick="location.reload()">Retry</button>
</div>
<script>
const token = "{token}";
const statusUrl = "/api/v1/webapp/" + token + "/_status";
let attempts = 0;
const maxAttempts = 60;

function updateStep(step, progress) {{
  document.querySelectorAll('.step').forEach((el, i) => {{
    el.className = 'step' + (i < step ? ' done' : i === step ? ' active' : '');
  }});
  document.getElementById('progress').style.width = progress + '%';
}}

async function poll() {{
  try {{
    const res = await fetch(statusUrl);
    const data = await res.json();
    if (data.status === 'running' && data.step === 'ready') {{
      updateStep(2, 100);
      setTimeout(() => location.reload(), 500);
      return;
    }} else if (data.status === 'running') {{
      updateStep(1, 66);
    }} else if (data.status === 'error') {{
      document.getElementById('error-msg').textContent = data.message || 'Failed to start agent environment';
      document.getElementById('error-msg').style.display = 'block';
      document.getElementById('retry-btn').style.display = 'inline-block';
      return;
    }} else {{
      updateStep(0, 33);
    }}
  }} catch(e) {{
    // Network error, keep polling
  }}
  attempts++;
  if (attempts < maxAttempts) {{
    setTimeout(poll, 2000);
  }} else {{
    document.getElementById('error-msg').textContent = 'Timed out waiting for agent to start';
    document.getElementById('error-msg').style.display = 'block';
    document.getElementById('retry-btn').style.display = 'inline-block';
  }}
}}
poll();
</script>
</body>
</html>"""
