// Shared presentation of the normalized API contract; no provider keys or polling.
function weatherNumber(value, unit='', fraction=false) {
  if (!Number.isFinite(value) || (fraction && (value < 0 || value > 1))) return '—';
  return (fraction ? (value * 100).toFixed(0) : value.toFixed(1)) + unit;
}
// Presentation thresholds for normalized moisture only, not provider grip states.
function surfaceMoisture(wetness) {
  if (!Number.isFinite(wetness) || wetness < 0 || wetness > 1) return '—';
  return wetness < .02 ? 'DRY' : wetness < .40 ? 'DAMP' : 'WET';
}
function engineerWeather(weather, live) {
  const current = weather?.current || {}, forecast = weather?.forecast || {};
  const nodes = Array.isArray(forecast.nodes) ? forecast.nodes : [];
  const complete = forecast.timeline_complete === true && nodes.length === 5 &&
    nodes.every((node, index) => node?.position_percent === index * 25);
  const label = index => complete ? ['Start','25 %','50 %','75 %','Finish'][index] : `Forecast ${index+1}`;
  const surface = surfaceMoisture(current.wetness_avg);
  return `<section aria-label="Weather">
    <div class="weather-heading"><h2>Weather</h2><span class="muted">${live === true ? 'Current conditions' : 'Last stored conditions'}</span></div>
    <div class="weather-current"><div class="surface ${surface === '—' ? '' : surface.toLowerCase()}"><span>Surface</span><strong>${surface}</strong></div>
    ${[['Air',weatherNumber(current.air_temp_c,' °C')],['Track',weatherNumber(current.track_temp_c,' °C')],
      ['Rain',weatherNumber(current.rain_intensity,' %',true)],['Track wetness',weatherNumber(current.wetness_avg,' %',true)]]
      .map(([name,value]) => `<div><span>${name}: </span><strong>${value}</strong></div>`).join('')}</div>
    ${nodes.length ? `<div class="forecast-heading">Forecast · temperature / rain chance</div><ol class="forecast-strip">${nodes.map((node,index) =>
      `<li><strong>${label(index)}</strong><span>${weatherNumber(node?.temperature_c,' °C')}</span><span class="rain-chance">${weatherNumber(node?.rain_chance,' %',true)}</span></li>`).join('')}</ol>
      ${complete ? '' : '<p class="muted">Source order only; forecast timeline positions unavailable.</p>'}` : '<p class="muted">Forecast unavailable.</p>'}
    <details id="weather-details"><summary id="weather-toggle">Weather details</summary>
      <p>Cloudiness: ${weatherNumber(current.cloudiness,' %',true)} · Wind: ${weatherNumber(current.wind_speed_mps,' m/s')} · Track wetness min / max: ${weatherNumber(current.wetness_min,' %',true)} / ${weatherNumber(current.wetness_max,' %',true)}</p>
      ${nodes.map((node,index) => {
        const direction = ['N','NE','E','SE','S','SW','W','NW'].includes(node?.wind_direction) ? node.wind_direction : '—';
        return `<p>${label(index)} · Humidity: ${weatherNumber(node?.humidity_percent,' %')} · Wind: ${esc(direction)} ${weatherNumber(node?.wind_speed_mps,' m/s')}</p>`;
      }).join('')}
    </details></section>`;
}
function weatherWidget(weather, live, compact=false) {
  const current = weather?.current || {};
  const forecast = weather?.forecast || {};
  const nodes = Array.isArray(forecast.nodes) ? forecast.nodes : [];
  const conditions = [
    ['Air', weatherNumber(current.air_temp_c, ' °C')],
    ['Track', weatherNumber(current.track_temp_c, ' °C')],
    ['Rain', weatherNumber(current.rain_intensity, ' %', true)],
    ['Track wetness', weatherNumber(current.wetness_avg, ' %', true)],
    ['Cloudiness', weatherNumber(current.cloudiness, ' %', true)],
    ['Wind', weatherNumber(current.wind_speed_mps, ' m/s')],
  ];
  const complete = forecast.timeline_complete === true && nodes.length === 5 &&
    nodes.every((node, index) => node?.position_percent === index * 25);
  const forecastHTML = `<h3>Forecast</h3>${nodes.length ? `<ul>${nodes.map((node, index) => {
    const n = node || {};
    const label = complete ? ['Start', '25 %', '50 %', '75 %', 'Finish'][index] : `Forecast ${index+1}`;
    const direction = ['N','NE','E','SE','S','SW','W','NW'].includes(n.wind_direction) ? n.wind_direction : '—';
    return `<li><strong>${label}</strong> · Temperature: ${weatherNumber(n.temperature_c, ' °C')} · Rain chance: ${weatherNumber(n.rain_chance, ' %', true)} · Humidity: ${weatherNumber(n.humidity_percent, ' %')} · Wind: ${esc(direction)} ${weatherNumber(n.wind_speed_mps, ' m/s')}</li>`;
  }).join('')}</ul>${complete ? '' : '<p class="muted">Source order only; forecast timeline positions unavailable.</p>'}` : '<p>Forecast unavailable.</p>'}`;
  return `<section aria-label="Weather"><h2>Weather</h2><h3>${live === true ? 'Current conditions' : 'Last stored conditions'}</h3>
    <p>${conditions.map(([label,value]) => `${label}: ${value}`).join(' · ')}</p>
    ${!compact && (Number.isFinite(current.wetness_min) || Number.isFinite(current.wetness_max)) ? `<p class="muted">Track wetness min / max: ${weatherNumber(current.wetness_min, ' %', true)} / ${weatherNumber(current.wetness_max, ' %', true)}</p>` : ''}
    ${compact ? `<details id="weather-forecast"><summary>Weather forecast</summary>${forecastHTML}</details>` : forecastHTML}</section>`;
}
