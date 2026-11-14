const canvas = document.querySelector('#scene');
const ctx = canvas.getContext('2d');

const layers = { upper: true, lower: true, context: true };
let yaw = 0, follow = true, showHitbox = true, animate = true, last = 0, drag = null;
const cars = [
  { id: 'ambulance', x: -12, y: -165, z: 5, dir: Math.PI / 2, color: '#18b9a5', speed: 22, w: 8, l: 18 },
  { id: 'car-a', x: 12, y: 92, z: 5, dir: -Math.PI / 2, color: '#617bde', speed: 13, w: 7, l: 15 },
  { id: 'car-b', x: -99, y: 5, z: -2, dir: 0, color: '#e49a42', speed: 8, w: 7, l: 15 }
];

// Координаты XY могут пересекаться на плане, но это не означает столкновение:
// физика и hitbox сравниваются только в одном транспортном уровне.
function collisionLayer(car) { return car.z > 0 ? 'bridge-deck' : 'lower-passage'; }
function shareCollisionDomain(first, second) { return collisionLayer(first) === collisionLayer(second); }
function underBridgeDeck(car) { return car.z < 0 && Math.abs(car.x) < 22 && Math.abs(car.y) < 205; }
function visibleOpacity(car) {
  if (car.z >= 0) return 1;
  const distanceToDeck = Math.abs(car.x);
  if (distanceToDeck <= 22) return 0;
  return Math.min(1, (distanceToDeck - 22) / 18);
}

function resize() {
  canvas.width = canvas.clientWidth * devicePixelRatio;
  canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
}

// Карта сверху: Z сдвигает объект, но не создаёт перспективу «из-под моста».
function project([x, y, z = 0]) {
  const c = Math.cos(yaw), s = Math.sin(yaw);
  const rx = x * c - y * s, ry = x * s + y * c;
  const scale = Math.min(canvas.clientWidth / 570, canvas.clientHeight / 430);
  return [canvas.clientWidth / 2 + rx * scale, canvas.clientHeight / 2 + ry * scale * .78 - z * scale * .46, scale];
}

function poly(points, fill, stroke, width = 1) {
  const ps = points.map(project);
  ctx.beginPath(); ctx.moveTo(ps[0][0], ps[0][1]);
  ps.slice(1).forEach(p => ctx.lineTo(p[0], p[1])); ctx.closePath();
  if (fill) { ctx.fillStyle = fill; ctx.fill(); }
  if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = width; ctx.stroke(); }
}
function rotate(x, y, a) { return [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)]; }
function shape(x, y, z, length, width, angle) {
  return [[-length / 2, -width / 2], [length / 2, -width / 2], [length / 2, width / 2], [-length / 2, width / 2]]
    .map(([px, py]) => { const [rx, ry] = rotate(px, py, angle); return [x + rx, y + ry, z]; });
}
function road(x, y, z, length, width, angle, color, lanes, visible) {
  if (!visible) return;
  poly(shape(x, y, z, length, width, angle), color, '#58717a', 1.2);
  for (let lane = 1; lane < lanes; lane++) {
    const offset = -width / 2 + width * lane / lanes;
    for (let at = -length / 2 + 7; at < length / 2; at += 20) {
      const a = rotate(at, offset - .42, angle), b = rotate(at + 9, offset - .42, angle);
      const c = rotate(at + 9, offset + .42, angle), d = rotate(at, offset + .42, angle);
      poly([[x + a[0], y + a[1], z + .03], [x + b[0], y + b[1], z + .03], [x + c[0], y + c[1], z + .03], [x + d[0], y + d[1], z + .03]], '#eff8f5');
    }
  }
}
function label(text, x, y, z, color = '#46636d') {
  const [sx, sy] = project([x, y, z]);
  ctx.font = '700 10px system-ui';
  ctx.fillStyle = '#fffffff0'; ctx.fillRect(sx - 3, sy - 10, ctx.measureText(text).width + 7, 14);
  ctx.fillStyle = color; ctx.fillText(text, sx, sy);
}
function drawCar(car) {
  const opacity = visibleOpacity(car);
  if (opacity === 0) return;
  ctx.save(); ctx.globalAlpha = opacity;
  const body = shape(car.x, car.y, car.z + .15, car.l, car.w, car.dir);
  poly(body, car.color, '#fff', 1.2);
  poly(shape(car.x, car.y, car.z + .2, car.l * .52, car.w * .72, car.dir), '#dff8f4aa');
  if (car.id === 'ambulance') {
    const [sx, sy] = project([car.x, car.y, car.z]);
    ctx.fillStyle = '#fff'; ctx.font = '900 10px system-ui'; ctx.fillText('+', sx - 3, sy + 3);
  }
  // Контур не рисуем при въезде под мост: он не должен создавать впечатление
  // ложного контакта с машиной на верхнем полотне.
  if (showHitbox && opacity > .92) { ctx.setLineDash([5, 3]); poly(body, null, '#ed6058', 1); }
  ctx.restore();
}
function building(x, y, w, h) { poly(shape(x, y, .1, w, h, 0), '#ded8cd', '#c1bbb0'); }
function signal(x, y) { const [sx, sy] = project([x, y, .2]); ctx.fillStyle = '#314c52'; ctx.fillRect(sx - 2, sy - 5, 4, 10); ctx.fillStyle = '#4ed2aa'; ctx.beginPath(); ctx.arc(sx, sy - 2, 1.5, 0, Math.PI * 2); ctx.fill(); }

function drawMap() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h); ctx.fillStyle = '#d7e8dd'; ctx.fillRect(0, 0, w, h);
  if (layers.context) {
    poly([[-300, -230, 0], [300, -230, 0], [300, 230, 0], [-300, 230, 0]], '#cde5c7');
    ctx.strokeStyle = '#edf3e9'; ctx.lineWidth = 1;
    for (let x = -250; x < 260; x += 34) { const a = project([x, -215, 0]); const b = project([x, 215, 0]); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
    for (let y = -205; y < 210; y += 34) { const a = project([-280, y, 0]); const b = project([280, y, 0]); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
    poly([[-235, -170, .03], [-55, -170, .03], [-55, -95, .03], [-235, -95, .03]], '#b9ddb1', '#98c993');
    label('парк и пешеходный контекст', -218, -132, .1, '#63845f');
    for (let x = 104; x < 250; x += 40) for (let y = -150; y < 165; y += 48) building(x, y, 24, 15);
    for (let x = -240; x < -100; x += 40) for (let y = 55; y < 175; y += 42) building(x, y, 21, 14);
  }
  if (layers.lower) {
    road(0, 8, -2, 480, 26, 0, '#80949a', 2, true);
    road(150, 53, -2, 122, 18, -.58, '#80949a', 1, true);
    label('нижний проезд · service / covered', -200, 29, -1.9, '#5e777d');
  }
  if (layers.upper) {
    [-130, -45, 45, 130].forEach(y => poly(shape(0, y, 1, 18, 9, 0), '#91a3a6', '#6f8589'));
    road(-12, 0, 5, 410, 20, Math.PI / 2, '#506a75', 2, true);
    road(12, 0, 5, 410, 20, Math.PI / 2, '#506a75', 2, true);
    // Физически отделённые встречные направления, а не одна общая полоса.
    poly(shape(0, 0, 5.07, 410, 4, Math.PI / 2), '#bdc9c9', '#eff6f4', 1);
    label('мост Беласар · 2 × 2 полосы', 22, -183, 5.1, '#315562');
  }
  cars.filter(c => c.z < 0 && layers.lower).forEach(drawCar);
  cars.filter(c => c.z > 0 && layers.upper).forEach(drawCar);
  if (layers.context) { signal(-28, -30); signal(28, 30); }
}
function frame(now) {
  const delta = Math.min((now - last) / 1000, .05); last = now;
  if (animate) cars.forEach(car => {
    if (car.z > 0) { car.y += car.speed * delta * (car.dir > 0 ? 1 : -1); if (car.y > 212) car.y = -212; if (car.y < -212) car.y = 212; }
    else { car.x += car.speed * delta; if (car.x > 225) car.x = -225; }
  });
  drawMap(); requestAnimationFrame(frame);
}

document.querySelectorAll('.layer').forEach(button => button.onclick = () => { const key = button.dataset.layer; layers[key] = !layers[key]; button.classList.toggle('active', layers[key]); });
document.querySelector('#hitbox').onchange = e => showHitbox = e.target.checked;
document.querySelector('#animate').onchange = e => animate = e.target.checked;
document.querySelector('#follow').onclick = e => { follow = true; yaw = 0; e.currentTarget.classList.add('selected'); document.querySelector('#overview').classList.remove('selected'); };
document.querySelector('#overview').onclick = e => { follow = false; yaw = -.13; e.currentTarget.classList.add('selected'); document.querySelector('#follow').classList.remove('selected'); };
canvas.onpointerdown = e => drag = [e.clientX, e.clientY];
canvas.onpointermove = e => { if (!drag) return; yaw += (e.clientX - drag[0]) * .004; drag = [e.clientX, e.clientY]; };
canvas.onpointerup = canvas.onpointerleave = () => drag = null;
new ResizeObserver(resize).observe(canvas); resize(); requestAnimationFrame(frame);
