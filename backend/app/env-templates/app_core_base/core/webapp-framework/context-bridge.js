// Webapp Context Bridge — do not remove or modify
// Handles bi-directional communication with the platform chat widget.
(function () {
  // ── Outgoing: schema.org context collection ──────────────────────────────
  function collectMicrodata() {
    var items = [];
    document.querySelectorAll('[itemscope]').forEach(function (el) {
      var item = {
        type: el.getAttribute('itemtype') || '',
        properties: {}
      };
      el.querySelectorAll('[itemprop]').forEach(function (prop) {
        var name = prop.getAttribute('itemprop');
        var value = (prop.tagName === 'INPUT' || prop.tagName === 'SELECT' || prop.tagName === 'TEXTAREA')
          ? prop.value
          : prop.textContent.trim();
        if (name && value !== '') {
          if (Object.prototype.hasOwnProperty.call(item.properties, name)) {
            if (!Array.isArray(item.properties[name])) {
              item.properties[name] = [item.properties[name]];
            }
            item.properties[name].push(value);
          } else {
            item.properties[name] = value;
          }
        }
      });
      if (Object.keys(item.properties).length > 0) {
        items.push(item);
      }
    });
    return items;
  }

  // ── Incoming: agent action dispatch ─────────────────────────────────────
  function handleWebappAction(action, data) {
    switch (action) {
      case 'refresh_page':
        window.location.reload();
        break;

      case 'reload_data':
        // Dispatch a custom event so your data-fetching code can listen and refetch.
        // data.endpoint contains the API path that should be re-fetched.
        window.dispatchEvent(new CustomEvent('webapp_reload_data', { detail: data }));
        break;

      case 'update_form':
        // data.form_id: the id attribute of the <form> element
        // data.values: { fieldName: newValue, ... }
        if (data && data.form_id && data.values) {
          var form = document.getElementById(data.form_id);
          if (form) {
            Object.keys(data.values).forEach(function (fieldName) {
              var field = form.elements[fieldName];
              if (field) {
                field.value = data.values[fieldName];
                field.dispatchEvent(new Event('input', { bubbles: true }));
                field.dispatchEvent(new Event('change', { bubbles: true }));
              }
            });
          }
        }
        break;

      case 'show_notification':
        // data.message: notification text
        // data.type: 'success' | 'error' | 'warning' | 'info'
        // Dispatch a custom event so your UI can render the notification.
        window.dispatchEvent(new CustomEvent('webapp_show_notification', { detail: data }));
        break;

      case 'navigate':
        // data.path: URL path to navigate to within the SPA
        if (data && data.path) {
          if (window.history && window.history.pushState) {
            window.history.pushState(null, '', data.path);
            window.dispatchEvent(new PopStateEvent('popstate'));
          } else {
            window.location.href = data.path;
          }
        }
        break;

      default:
        // Unknown action — dispatch as a generic custom event so custom handlers can react.
        window.dispatchEvent(new CustomEvent('webapp_action_' + action, { detail: data }));
        break;
    }
  }

  // ── Message listener: handles both directions ────────────────────────────
  window.addEventListener('message', function (event) {
    if (!event.data) return;

    // Outgoing: respond to context collection requests from the platform
    if (event.data.type === 'request_page_context') {
      var context = {
        url: window.location.href,
        title: document.title,
        microdata: collectMicrodata()
      };
      event.source.postMessage({ type: 'page_context_response', context: context }, event.origin || '*');
      return;
    }

    // Incoming: execute action commands sent by the agent
    if (event.data.type === 'webapp_action') {
      handleWebappAction(event.data.action, event.data.data || {});
    }
  });
})();
