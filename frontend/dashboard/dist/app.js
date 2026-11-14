const map = document.querySelector('#cityMap');
const ambulancePath = document.querySelector('#ambulancePath');
const ambulance = document.querySelector('#ambulanceMarker');
const police = document.querySelector('#policeMarker');
const play = document.querySelector('#play');
const reset = document.querySelector('#reset');
const progress = document.querySelector('#progress');
const seconds = document.querySelector('#seconds');
const eta = document.querySelector('#eta');
const progressText = document.querySelector('#progressText');
const timeline = [...document.querySelectorAll('.timeline li')];

let playing = false;
let elapsed = 0;
let lastFrame = 0;
let zoom = 1;
const duration = 400;

function place(marker, path, ratio) {
  const point = path.getPointAtLength(path.getTotalLength() * ratio);
  marker.setAttribute('transform', `translate(${point.x} ${point.y})`);
}

function format(value) {
  const rest = Math.max(0, duration - Math.round(value));
  return `${String(Math.floor(rest / 60)).padStart(2, '0')}:${String(rest % 60).padStart(2, '0')}`;
}

function render() {
  const ratio = Math.min(elapsed / duration, 1);
  place(ambulance, ambulancePath, ratio);
  place(police, ambulancePath, Math.min(ratio * 1.35 + .13, 1));
  progress.style.width = `${ratio * 100}%`;
  seconds.textContent = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(Math.floor(elapsed % 60)).padStart(2, '0')} / 06:40`;
  eta.textContent = format(elapsed);
  const stage = Math.min(4, Math.floor(ratio * 5));
  timeline.forEach((item, index) => item.classList.toggle('active', index <= stage));
  progressText.textContent = `шаг ${stage + 1} / 5`;
  if (ratio === 1) { playing = false; play.innerHTML = '✓ <span>Экипаж прибыл</span>'; }
}

function tick(time) {
  if (!playing) return;
  if (!lastFrame) lastFrame = time;
  elapsed = Math.min(duration, elapsed + (time - lastFrame) / 38);
  lastFrame = time;
  render();
  if (elapsed < duration) requestAnimationFrame(tick);
}

play.addEventListener('click', () => {
  if (elapsed >= duration) elapsed = 0;
  playing = !playing;
  lastFrame = 0;
  play.innerHTML = playing ? 'Ⅱ <span>Пауза сценария</span>' : '▶ <span>Продолжить</span>';
  if (playing) requestAnimationFrame(tick);
});
reset.addEventListener('click', () => { playing = false; elapsed = 0; play.innerHTML = '▶ <span>Запустить сценарий</span>'; render(); });
document.querySelector('#zoomIn').addEventListener('click', () => { zoom = Math.min(1.45, zoom + .1); map.style.transform = `scale(${zoom})`; });
document.querySelector('#zoomOut').addEventListener('click', () => { zoom = Math.max(.85, zoom - .1); map.style.transform = `scale(${zoom})`; });
document.querySelector('#layers').addEventListener('click', (event) => { const hidden = map.classList.toggle('hide-buildings'); event.currentTarget.setAttribute('aria-pressed', String(!hidden)); event.currentTarget.lastElementChild.textContent = hidden ? 'Объекты: выкл.' : 'Объекты'; });
document.querySelectorAll('.resource').forEach(button => button.addEventListener('click', () => document.querySelectorAll('.resource').forEach(item => item.classList.toggle('selected', item === button))));
document.querySelector('#clock').textContent = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date());
place(ambulance, ambulancePath, 0); place(police, ambulancePath, .13); render();
