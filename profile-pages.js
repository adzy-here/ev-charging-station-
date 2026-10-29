(() => {
  'use strict';

  const $ = (selector) => document.querySelector(selector);
  const expectedRole = document.body.dataset.profileRole;
  const themeKey = 'zero_latency_theme';
  const themeModes = ['system', 'light', 'dark'];
  const apiBase = (window.ZERO_LATENCY_API_BASE || localStorage.getItem('zero_latency_api_base') || 'http://localhost:8000').replace(/\/$/, '');
  const returnPage = new URLSearchParams(window.location.search).get('return') === 'index'
    ? 'index%20(5).html'
    : 'zero_latency_interactive_preview_human_copy.html';
  const money = (value) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(Number(value || 0));
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);

  function applyTheme(mode) {
    const selected = themeModes.includes(mode) ? mode : 'system';
    const dark = selected === 'dark' || (selected === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.dataset.themeMode = selected;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = dark ? '#121b23' : '#f8f7f1';
    const button = $('[data-theme-toggle]');
    if (button) {
      const next = themeModes[(themeModes.indexOf(selected) + 1) % themeModes.length];
      const label = selected[0].toUpperCase() + selected.slice(1);
      button.querySelector('.theme-toggle-label').textContent = label;
      button.setAttribute('aria-label', `Color theme: ${label}. Activate to switch to ${next[0].toUpperCase() + next.slice(1)} mode`);
      button.title = `Color theme: ${label}`;
    }
  }

  applyTheme(localStorage.getItem(themeKey) || 'system');
  $('[data-theme-toggle]').addEventListener('click', () => {
    const current = themeModes.includes(localStorage.getItem(themeKey)) ? localStorage.getItem(themeKey) : 'system';
    const next = themeModes[(themeModes.indexOf(current) + 1) % themeModes.length];
    localStorage.setItem(themeKey, next);
    applyTheme(next);
  });
  window.addEventListener('storage', (event) => {
    if (event.key === themeKey) applyTheme(event.newValue || 'system');
  });

  async function request(path, options = {}) {
    const token = localStorage.getItem('zero_latency_token');
    const response = await fetch(`${apiBase}${path}`, {
      ...options,
      headers: { ...(options.headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(result.detail || 'Unable to load profile data.');
      error.status = response.status;
      throw error;
    }
    return result;
  }

  function setStatus(message, kind = '') {
    const status = $('#page-status');
    status.textContent = message;
    status.classList.toggle('error', kind === 'error');
    status.hidden = !message;
  }

  function clearSession() {
    localStorage.removeItem('zero_latency_token');
    localStorage.removeItem('zero_latency_role');
  }

  function returnToHome(event) {
    const target = new URL(event.currentTarget.href, window.location.href);
    const referrer = document.referrer ? new URL(document.referrer) : null;
    if (event.currentTarget.dataset.profileReturn === 'menu') {
      sessionStorage.setItem('zero_latency_open_profile_menu', 'true');
    }
    if (!referrer || referrer.origin !== target.origin || referrer.pathname !== target.pathname || window.history.length <= 1) return;

    event.preventDefault();
    window.history.back();
  }

  function dateLabel(value, includeTime = false) {
    if (!value) return 'Not available';
    const normalized = String(value).includes('T') ? String(value) : String(value).replace(' ', 'T') + 'Z';
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return escapeHtml(value);
    return new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata', year: 'numeric', month: 'short', day: 'numeric',
      ...(includeTime ? { hour: 'numeric', minute: '2-digit' } : {}),
    }).format(date);
  }

  function durationLabel(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return 'No charging session';
    const total = Math.max(0, Math.floor(Number(seconds)));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    if (hours) return `${hours} hr ${minutes} min`;
    if (minutes) return `${minutes} min ${remainder} sec`;
    return `${remainder} sec`;
  }

  function renderHostOverview(data) {
    const summary = data.summary || {};
    const metrics = [
      ['Host earnings', money(summary.host_earnings)],
      ['Gross revenue', money(summary.gross_revenue)],
      ['Completed sessions', Number(summary.completed_sessions || 0).toLocaleString('en-IN')],
      ['Energy delivered', `${Number(summary.energy_kwh || 0).toFixed(1)} kWh`],
    ];
    $('#host-metrics').innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join('');

    const points = data.earnings_daily || [];
    const maximum = Math.max(0, ...points.map((point) => Number(point.host_earnings || 0)));
    const hasPayouts = points.some((point) => Number(point.transaction_count || 0) > 0);
    $('#earnings-chart').innerHTML = hasPayouts ? points.map((point) => {
      const amount = Number(point.host_earnings || 0);
      const height = maximum > 0 && amount > 0 ? Math.max(5, amount / maximum * 100) : 0;
      const day = String(point.day || '').slice(8, 10) || '—';
      return `<div class="chart-day" title="${escapeHtml(dateLabel(point.day))}: ${escapeHtml(money(amount))}"><div class="chart-bar" role="img" aria-label="${escapeHtml(dateLabel(point.day))}: ${escapeHtml(money(amount))}" style="--bar-height:${height}%"></div><span>${escapeHtml(day)}</span></div>`;
    }).join('') : '<p class="empty-state chart-empty">No completed host payouts are available yet.</p>';

    const trend = data.earnings_trend;
    if (!trend || trend.previous_7_days == null || trend.current_7_days == null || !['up', 'down', 'flat'].includes(trend.direction)) {
      $('#earnings-trend').innerHTML = '<strong>Trend unavailable</strong><span>Both 7-day periods need completed payouts before a comparison is shown.</span>';
      return;
    }
    const direction = trend.direction;
    const arrow = direction === 'up' ? '&#8593;' : direction === 'down' ? '&#8595;' : '&#8594;';
    const percentage = trend.change_percent == null ? '' : ` (${Math.abs(Number(trend.change_percent)).toFixed(1)}%)`;
    const trendClass = direction === 'up' ? 'up' : direction === 'down' ? 'down' : 'flat';
    $('#earnings-trend').innerHTML = `<strong class="${trendClass}">${arrow} ${direction === 'flat' ? 'No change' : direction === 'up' ? 'Increase' : 'Decrease'}${percentage}</strong><span>Previous 7 days: ${escapeHtml(money(trend.previous_7_days))} · Current 7 days: ${escapeHtml(money(trend.current_7_days))}</span>`;
  }

  function renderHostStations(stations) {
    const list = $('#station-list');
    if (!stations.length) {
      list.innerHTML = '<div class="empty-state">No stations are listed on this Host account yet.</div>';
      return;
    }
    list.innerHTML = stations.map((station) => {
      const slots = Array.isArray(station.slots) ? station.slots : [];
      const availableSlots = slots.filter((slot) => !slot.is_booked).length;
      const availability = station.status !== 'active'
        ? station.status || 'Not available'
        : availableSlots ? `${availableSlots} upcoming slot${availableSlots === 1 ? '' : 's'}` : 'No upcoming availability';
      const availabilityClass = station.status === 'active' && availableSlots ? 'is-available' : 'is-unavailable';
      return `<article class="record station-record"><div class="record-top"><div><h3>${escapeHtml(station.title)}</h3><p>${escapeHtml(station.address || 'Location not listed')}</p></div><strong class="record-value">${escapeHtml(money(station.host_earnings))}</strong></div><dl class="record-details"><div><dt>Live availability</dt><dd><span class="availability-badge ${availabilityClass}">${escapeHtml(availability)}</span></dd></div><div><dt>Station status</dt><dd>${escapeHtml(station.status || 'Not available')}</dd></div><div><dt>Connector</dt><dd>${escapeHtml(station.connector_type || 'Not available')}</dd></div><div><dt>Rate</dt><dd>${escapeHtml(money(station.price_rate))} ${station.pricing_type === 'per_hour' ? 'per hour' : 'per kWh'}</dd></div><div><dt>Completed sessions</dt><dd>${Number(station.completed_sessions || 0)}</dd></div><div><dt>Energy delivered</dt><dd>${Number(station.energy_kwh || 0).toFixed(1)} kWh</dd></div><div><dt>Total earnings</dt><dd>${escapeHtml(money(station.host_earnings))}</dd></div></dl></article>`;
    }).join('');
  }

  function renderReviews(reviews) {
    const list = $('#review-list');
    if (!reviews.length) {
      list.innerHTML = '<div class="empty-state">No reviews yet.</div>';
      return;
    }
    list.innerHTML = reviews.map((review) => {
      const rating = Math.max(0, Math.min(5, Math.round(Number(review.rating) || 0)));
      const stars = '★'.repeat(rating) + '☆'.repeat(5 - rating);
      return `<article class="record"><div class="record-top"><div><h3>${escapeHtml(review.station_title)}</h3><p>${escapeHtml(review.reviewer_name || 'Consumer')} · ${escapeHtml(dateLabel(review.created_at))}</p></div><span class="record-value rating" aria-label="${rating} out of 5 stars"><span class="rating-stars" aria-hidden="true">${stars}</span> ${rating}/5</span></div><p>${escapeHtml(review.comment || 'No written feedback.')}</p></article>`;
    }).join('');
  }

  function renderHostStrikes(strikes) {
    const list = $('#host-strike-list');
    if (!strikes.length) {
      list.innerHTML = '<div class="empty-state">No consumer strikes recorded.</div>';
      return;
    }
    list.innerHTML = strikes.map((strike) => `<article class="record record-strike"><div class="record-top"><div><h3>${escapeHtml(strike.consumer_name || 'Consumer')}</h3><p>${escapeHtml(strike.station_title)} · ${escapeHtml(dateLabel(strike.created_at))}</p></div><strong class="record-value">${Number(strike.strike_count || 0)}/3 strikes</strong></div><dl class="record-details"><div><dt>Reason</dt><dd>${escapeHtml(strike.reason)}</dd></div><div><dt>Booking</dt><dd>${escapeHtml(strike.booking_id)}</dd></div></dl></article>`).join('');
  }

  function bookingPrice(booking) {
    if (booking.total_amount != null) {
      return { label: booking.session_interrupted ? 'Final estimated amount' : 'Price charged', value: money(booking.total_amount) };
    }
    if (booking.status === 'charging' && booking.estimated_amount != null) {
      return { label: 'Estimated so far', value: money(booking.estimated_amount) };
    }
    return { label: 'Price', value: 'Not available' };
  }

  function renderBookings(bookings) {
    const list = $('#booking-list');
    if (!bookings.length) {
      list.innerHTML = `<div class="empty-state booking-empty"><strong>No bookings yet.</strong><span>Find a charging station to start your first booking.</span><a class="profile-action-link" href="${escapeHtml(returnPage)}#explore">Explore stations</a></div>`;
      return;
    }
    const sorted = [...bookings].sort((a, b) => new Date(String(b.created_at || '').replace(' ', 'T') + 'Z') - new Date(String(a.created_at || '').replace(' ', 'T') + 'Z'));
    list.innerHTML = sorted.map((booking) => {
      const price = bookingPrice(booking);
      const duration = booking.charged_duration_seconds == null ? 'No charging session' : durationLabel(Number(booking.charged_duration_seconds));
      const priceNote = price.label === 'Price' ? price.value : `${price.label}: ${price.value}`;
      const status = String(booking.status || 'unknown');
      const statusLabel = status.replace(/_/g, ' ');
      const statusClass = status === 'completed' ? 'is-completed' : status === 'charging' ? 'is-active' : status === 'cancelled' ? 'is-cancelled' : '';
      const energy = booking.energy_consumed ?? booking.estimated_energy_kwh;
      const energyLabel = booking.energy_consumed != null ? 'Energy delivered' : booking.estimated_energy_kwh != null ? 'Estimated energy' : 'Energy delivered';
      const energyValue = energy == null ? 'Not available' : `${Number(energy).toFixed(2)} kWh`;
      const baseRate = booking.base_rate_at_start == null
        ? 'Not available'
        : `${money(booking.base_rate_at_start)} / ${booking.pricing_type_at_start === 'per_hour' ? 'hour' : 'kWh'}`;
      return `<details class="booking-row"><summary><span class="booking-summary"><strong>${escapeHtml(booking.station_title || 'Charging station')}</strong><span class="booking-summary-meta">${escapeHtml(dateLabel(booking.created_at, true))}<span class="booking-status ${statusClass}">${escapeHtml(statusLabel)}</span></span></span><span class="booking-price">${escapeHtml(priceNote)}</span></summary><div class="booking-details"><dl class="record-details"><div><dt>Station name</dt><dd>${escapeHtml(booking.station_title || 'Not available')}</dd></div><div><dt>Booking date and time</dt><dd>${escapeHtml(dateLabel(booking.slot_start || booking.created_at, true))}${booking.slot_end ? ` – ${escapeHtml(dateLabel(booking.slot_end, true))}` : ''}</dd></div><div><dt>Booking or session status</dt><dd>${escapeHtml(statusLabel)}</dd></div><div><dt>Charged duration</dt><dd>${escapeHtml(duration)}${booking.status === 'charging' ? ' · in progress' : ''}</dd></div><div><dt>${energyLabel}</dt><dd>${escapeHtml(energyValue)}</dd></div><div><dt>Base rate</dt><dd>${escapeHtml(baseRate)}</dd></div><div><dt>${escapeHtml(price.label)}</dt><dd>${escapeHtml(price.value)}</dd></div><div><dt>Booking reference</dt><dd>${escapeHtml(booking.id || 'Not available')}</dd></div></dl>${booking.status === 'charging' ? '<p class="booking-note">Energy, duration, and price are current estimates and may change before the session is finalized.</p>' : booking.session_interrupted ? '<p class="booking-note">This interrupted session amount is an estimate, not a metered final charge.</p>' : ''}</div></details>`;
    }).join('');
  }

  function renderProfile(user, data) {
    const roleName = expectedRole === 'host' ? 'Host' : 'Consumer';
    const displayName = String(user.name || '').trim();
    $('#profile-name').textContent = displayName ? `Welcome back, ${displayName}` : 'Welcome back';
    $('#profile-email').textContent = user.email ? `Email: ${user.email}` : 'Email unavailable';
    if (expectedRole === 'host') {
      renderHostOverview(data);
      renderHostStations(data.stations || []);
      renderReviews(data.reviews || []);
      renderHostStrikes(data.strikes || []);
    } else {
      const strikes = data.strikes || {};
      $('#consumer-strike-count').textContent = `${Number(strikes.strike_count || 0)} ${Number(strikes.strike_count || 0) === 1 ? 'strike' : 'strikes'}`;
      const list = $('#consumer-strike-list');
      if (!strikes.strikes?.length) {
        list.innerHTML = '<div class="empty-state no-strikes">NO STRIKES YET</div>';
      } else {
        list.innerHTML = strikes.strikes.map((strike) => `<article class="record record-strike"><div class="record-top"><div><h3>${escapeHtml(strike.station_title)}</h3><p>${escapeHtml(dateLabel(strike.created_at))} · Booking ${escapeHtml(strike.booking_id)}</p></div></div><dl class="record-details"><div><dt>Reason</dt><dd>${escapeHtml(strike.reason)}</dd></div></dl></article>`).join('');
      }
      renderBookings(data.bookings || []);
    }
    document.title = `${roleName} Profile | Zero Latency`;
    $('#profile-view').hidden = false;
  }

  async function loadProfile() {
    setStatus('Loading your profile…');
    $('#profile-view').hidden = true;
    try {
      const token = localStorage.getItem('zero_latency_token');
      if (!token) throw Object.assign(new Error('Please log in to view this profile.'), { status: 401 });
      const user = await request('/auth/me');
      if (!['host', 'consumer'].includes(user.role)) throw Object.assign(new Error('This account does not have a supported profile role.'), { status: 403 });
      if (user.role !== expectedRole) {
        window.location.replace(user.role === 'host' ? 'host-profile.html' : 'consumer-profile.html');
        return;
      }
      localStorage.setItem('zero_latency_role', user.role);
      const data = expectedRole === 'host'
        ? await request('/analytics/host')
        : { strikes: await request('/consumer/strikes/mine'), bookings: await request('/bookings/mine') };
      renderProfile(user, data);
      setStatus('');
    } catch (error) {
      if (error.status === 401 || error.status === 403) clearSession();
      setStatus(error.status === 401 || error.status === 403 ? 'Your session is unavailable or does not have access to this profile. Please sign in again.' : error.message || 'Unable to load profile data.', 'error');
      $('#retry-profile').hidden = false;
    }
  }

  $('#logout-profile').addEventListener('click', async () => {
    try { await request('/auth/logout', { method: 'POST' }); } catch (_) { /* Clear the local session even if revocation is unavailable. */ }
    clearSession();
    window.location.assign(`${returnPage}#account`);
  });
  document.querySelectorAll('a[href*="index%20(5).html"]').forEach((link) => {
    const target = new URL(link.href);
    link.href = `${returnPage}${target.hash || '#home'}`;
  });
  document.querySelectorAll('[data-profile-return]').forEach((link) => {
    link.href = `${returnPage}${link.dataset.profileReturn === 'menu' ? '#profile-menu' : '#home'}`;
    link.addEventListener('click', returnToHome);
  });
  $('#retry-profile').addEventListener('click', loadProfile);
  loadProfile();
})();
