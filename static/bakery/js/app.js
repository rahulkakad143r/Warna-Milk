(function () {
  "use strict";

  /* ---------------------------------------------------------- helpers */

  function csrfToken() {
    const el = document.querySelector("[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }

  function money(value) {
    return Number(value || 0).toFixed(2);
  }

  function compact(value) {
    const n = Number(value || 0);
    if (n >= 10000000) return (n / 10000000).toFixed(2) + "Cr";
    if (n >= 100000) return (n / 100000).toFixed(2) + "L";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return n.toFixed(0);
  }

  function css(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  /* ---------------------------------------------------------- sidebar */

  function initNav() {
    const toggle = document.querySelector("[data-nav-toggle]");
    const sidebar = document.getElementById("sidebar");
    const scrim = document.querySelector(".scrim");
    if (!toggle || !sidebar) return;

    function setOpen(open) {
      sidebar.classList.toggle("open", open);
      if (scrim) scrim.hidden = !open;
    }
    toggle.addEventListener("click", function () {
      setOpen(!sidebar.classList.contains("open"));
    });
    if (scrim) scrim.addEventListener("click", function () { setOpen(false); });
  }

  /* ---------------------------------------------------- live totals */

  function initCheckoutTotals() {
    const form = document.getElementById("checkout-form");
    const subtotalEl = document.getElementById("js-subtotal");
    if (!form || !subtotalEl) return;

    const base = parseFloat(subtotalEl.textContent) || 0;
    const lines = Array.prototype.map.call(
      document.querySelectorAll(".checkout-line"),
      function (row) {
        return {
          total: parseFloat(row.getAttribute("data-line-total")) || 0,
          discount: parseFloat(row.getAttribute("data-line-discount")) || 0,
          rate: parseFloat(row.getAttribute("data-tax-rate")) || 0
        };
      }
    );
    const out = {
      discount: document.getElementById("js-discount"),
      tax: document.getElementById("js-tax"),
      total: document.getElementById("js-total")
    };

    function recalc() {
      let discount = 0;
      let tax = 0;
      lines.forEach(function (line) {
        const lineDiscount = Math.min(Math.max(line.discount, 0), line.total);
        discount += lineDiscount;
        const lineTax = ((line.total - lineDiscount) * line.rate) / 100;
        tax += Math.round((lineTax + Number.EPSILON) * 100) / 100;
      });

      if (out.discount) out.discount.textContent = money(discount);
      if (out.tax) out.tax.textContent = money(tax);
      if (out.total) out.total.textContent = money(base - discount + tax);
    }

    recalc();
  }

  /* ----------------------------------------------------- cart badge */

  function setCartCount(count, total, tax, grandTotal) {
    const badge = document.getElementById("cart-badge");
    const countEl = document.getElementById("cart-count");
    const totalEl = document.getElementById("cart-total");
    const taxEl = document.getElementById("cart-tax");
    const grandEl = document.getElementById("cart-grand-total");
    if (badge) {
      badge.textContent = count;
      badge.classList.toggle("is-zero", !count);
    }
    if (countEl) countEl.textContent = count;
    if (totalEl) totalEl.textContent = "\u20b9" + total;
    if (taxEl) taxEl.textContent = "\u20b9" + (tax || "0.00");
    if (grandEl) grandEl.textContent = "\u20b9" + (grandTotal || total);
  }

  function refreshCartPanel() {
    const panel = document.getElementById("cart-panel");
    if (!panel) return;
    fetch(window.location.href, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html) return;
        const doc = new DOMParser().parseFromString(html, "text/html");
        const fresh = doc.getElementById("cart-panel");
        if (fresh) panel.replaceWith(fresh);
      })
      .catch(function () {});
  }

  function initAjaxCart() {
    document.querySelectorAll(".pcard-form").forEach(function (form) {
      const button = form.querySelector("button");
      if (!button) return;

      form.addEventListener("submit", function (event) {
        event.preventDefault();
        button.disabled = true;
        fetch(form.action, {
          method: "POST",
          headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken() },
          body: new FormData(form)
        })
          .then(function (r) {
            if (!r.ok) throw new Error("bad response");
            return r.json();
          })
          .then(function (data) {
            if (!data.ok) throw new Error("rejected");
            setCartCount(data.cart_count, data.cart_total, data.cart_tax,
              data.cart_grand_total);
            refreshCartPanel();
            button.textContent = "Added \u2713";
            setTimeout(function () {
              button.textContent = "Add to Cart";
              button.disabled = false;
            }, 900);
          })
          .catch(function () { form.submit(); });
      });
    });
  }

  /* ---------------------------------------------------------- charts */

  function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || canvas.parentNode.clientWidth || 600;
    const height = Number(canvas.getAttribute("height")) || 240;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.height = height + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx: ctx, w: width, h: height };
  }

  function barChart(canvasId, data, valueKey, labelKey) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !data || !data.length) return;

    const g = setupCanvas(canvas);
    const ctx = g.ctx;
    const padL = 46, padR = 12, padT = 14, padB = 30;
    const plotW = g.w - padL - padR;
    const plotH = g.h - padT - padB;

    const values = data.map(function (d) { return Number(d[valueKey] || 0); });
    const max = Math.max.apply(null, values.concat([1]));
    const ink = css("--ink", "#2B1F17");
    const muted = css("--muted", "#8B7A68");
    const line = css("--line", "#E4D8C4");
    const top = css("--gold", "#C99A54");
    const bottom = css("--caramel", "#A9713C");

    ctx.font = "11px " + (getComputedStyle(document.body).fontFamily || "sans-serif");
    ctx.strokeStyle = line;
    ctx.lineWidth = 1;
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const y = padT + (plotH / steps) * i;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(compact(max - (max / steps) * i), padL - 8, y);
    }

    const slot = plotW / data.length;
    const barW = Math.min(38, slot * 0.6);
    const gradient = ctx.createLinearGradient(0, padT, 0, padT + plotH);
    gradient.addColorStop(0, top);
    gradient.addColorStop(1, bottom);

    ctx.textAlign = "center";
    data.forEach(function (d, i) {
      const value = values[i];
      const h = value > 0 ? Math.max(3, (value / max) * plotH) : 2;
      const x = padL + slot * i + (slot - barW) / 2;
      const y = padT + plotH - h;

      ctx.fillStyle = value > 0 ? gradient : line;
      ctx.beginPath();
      const r = Math.min(5, barW / 2);
      ctx.moveTo(x, y + h);
      ctx.lineTo(x, y + r);
      ctx.quadraticCurveTo(x, y, x + r, y);
      ctx.lineTo(x + barW - r, y);
      ctx.quadraticCurveTo(x + barW, y, x + barW, y + r);
      ctx.lineTo(x + barW, y + h);
      ctx.closePath();
      ctx.fill();

      if (value > 0) {
        ctx.fillStyle = ink;
        ctx.textBaseline = "bottom";
        ctx.fillText(compact(value), x + barW / 2, y - 4);
      }
      ctx.fillStyle = muted;
      ctx.textBaseline = "top";
      ctx.fillText(String(d[labelKey]), x + barW / 2, padT + plotH + 8);
    });
  }

  function donutChart(canvasId, rows) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !rows || !rows.length) return;

    const g = setupCanvas(canvas);
    const ctx = g.ctx;
    const colors = [
      css("--caramel", "#A9713C"),
      css("--gold", "#C99A54"),
      css("--brown", "#6B472C"),
      css("--cocoa", "#4A3225")
    ];

    const values = rows.map(function (r) { return Number(r.amount || 0); });
    const total = values.reduce(function (a, b) { return a + b; }, 0);
    if (total <= 0) {
      ctx.fillStyle = css("--muted", "#8B7A68");
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = "13px sans-serif";
      ctx.fillText("No payments yet", g.w / 2, g.h / 2);
      return;
    }

    const cx = g.w / 2, cy = g.h / 2;
    const outer = Math.min(g.w, g.h) / 2 - 6;
    const inner = outer * 0.6;
    let start = -Math.PI / 2;

    rows.forEach(function (row, i) {
      const slice = (values[i] / total) * Math.PI * 2;
      ctx.beginPath();
      ctx.arc(cx, cy, outer, start, start + slice);
      ctx.arc(cx, cy, inner, start + slice, start, true);
      ctx.closePath();
      ctx.fillStyle = colors[i % colors.length];
      ctx.fill();
      start += slice;
    });

    ctx.fillStyle = css("--espresso", "#2F2018");
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "600 15px " + (getComputedStyle(document.body).fontFamily || "sans-serif");
    ctx.fillText("\u20b9" + compact(total), cx, cy - 6);
    ctx.fillStyle = css("--muted", "#8B7A68");
    ctx.font = "11px " + (getComputedStyle(document.body).fontFamily || "sans-serif");
    ctx.fillText("collected", cx, cy + 11);
  }

  function initCharts() {
    const week = window.GK_CHART && window.GK_CHART.points;
    if (week) {
      barChart("week-chart", week.map(function (p) {
        return { label: p.day, value: p.net };
      }), "value", "label");
    }

    const charts = window.GK_CHARTS;
    if (charts) {
      if (charts.month) {
        barChart("month-chart", charts.month.map(function (r) {
          return { label: String(r.day).slice(5), value: r.net };
        }), "value", "label");
      }
      if (charts.sales) {
        barChart("sales-chart", charts.sales.map(function (r) {
          return { label: r.label, value: r.revenue };
        }), "value", "label");
        barChart("purchase-chart", charts.sales.map(function (r) {
          return { label: r.label, value: r.purchase_total };
        }), "value", "label");
      }
      if (charts.payment) {
        donutChart("pay-chart", charts.payment);
      }
    }
  }

  /* ------------------------------------------------------- date/time */

  /* Every page renders {% now %} server-side so the stamp is right even with
     JS off; this only takes over to tick the seconds. */
  function initClock() {
    const el = document.querySelector("[data-clock]");
    if (!el) return;
    const dateEl = el.querySelector("[data-clock-date]");
    const timeEl = el.querySelector("[data-clock-time]");
    if (!dateEl || !timeEl) return;

    const pad = function (n) { return String(n).padStart(2, "0"); };
    function tick() {
      const now = new Date();
      dateEl.textContent = now.toLocaleDateString(undefined, {
        weekday: "short", day: "2-digit", month: "short", year: "numeric"
      });
      timeEl.textContent = pad(now.getHours()) + ":" + pad(now.getMinutes()) +
        ":" + pad(now.getSeconds());
    }
    tick();
    /* align to the next whole second so the digits do not visibly jump */
    setTimeout(function () {
      tick();
      setInterval(tick, 1000);
    }, 1000 - (Date.now() % 1000));
  }

  /* ----------------------------------------------------------- boot */

  document.addEventListener("DOMContentLoaded", function () {
    initNav();
    initClock();
    initCheckoutTotals();
    initAjaxCart();
    initCharts();
  });

  window.addEventListener("resize", function () {
    initCharts();
  });
})();
