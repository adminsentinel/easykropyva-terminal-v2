# EasyKropyva Terminal Backend v1.6.2 - Simplified elevation profile
import math
import time
import os
import random
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# Глобальні дані (сховище в пам'яті)
nodes = {}
targets = {}
home_point = None

# --- ГЕОМЕТРІЯ ТА ФІЗИКА ---

def haversine_distance(lat1, lng1, lat2, lng2):
    """Розрахунок відстані між точками (метри)"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlamb = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlamb/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

def sample_line(lat1, lng1, lat2, lng2, samples=30):
    """Генерує список точок на лінії між A та B"""
    points = []
    for i in range(samples):
        f = i / (samples - 1)
        lat = lat1 + (lat2 - lat1) * f
        lng = lng1 + (lng2 - lng1) * f
        points.append((lat, lng))
    return points

def calculate_diffraction_loss(h, d1, d2, freq_mhz):
    """
    Спрощений розрахунок втрат на дифракцію (Knife-edge).
    h: висота перешкоди над лінією LOS (може бути від'ємною).
    """
    if h <= -0.6: return 0.0
    v = h * math.sqrt(2 * (d1 + d2) / ( (300 / freq_mhz) * d1 * d2 ))
    if v <= -1: return 0.0
    return 6.9 + 20 * math.log10(math.sqrt((v - 0.1)**2 + 1) + v - 0.1)

def compute_los(elevations, base_height_a=0.5, base_height_b=0.5, freq_mhz=433.0, distance_m=0, vegetation_height_m=0.5):
    """
    Розрахунок прямої видимості та зони Френеля.
    Додано модель затухання трави (ITU-R P.833 спрощена).
    """
    n = len(elevations)
    if n < 2: return {'пряма_видимість': True, 'terrain_los': True}

    # Модель затухання трави (спрощена ITU-R P.833)
    # Для 433 MHz: ~0.15 dB/m, для 900 MHz: ~0.3 dB/m
    veg_attenuation_db_per_m = 0.15 if freq_mhz < 500 else 0.3
    vegetation_loss = veg_attenuation_db_per_m * vegetation_height_m * (distance_m / 1000.0) if distance_m > 0 else 0

    elev_a = elevations[0] + base_height_a
    elev_b = elevations[-1] + base_height_b

    # Копіюємо список, щоб не мутувати оригінальні дані
    elevs = list(elevations)
    distance_km = distance_m / 1000.0
    
    for i in range(n):
        f = i / (n - 1)
        dist_from_start_km = f * distance_km
        # Висота "просідання" горизонту через кривизну
        earth_drop = (dist_from_start_km * (distance_km - dist_from_start_km)) / 12.74
        elevs[i] += earth_drop

    los_clear = True
    fresnel_clear = True
    max_obstruction = 0.0
    obstruction_idx = -1

    for i in range(1, n - 1):
        f = i / (n - 1)
        line_height = elev_a + f * (elev_b - elev_a)
        terrain = elevs[i]

        # Оптична видимість (Чи рельєф перекриває пряму лінію)
        if terrain > line_height:
            los_clear = False
            diff = terrain - line_height
            if diff > max_obstruction:
                max_obstruction = diff
                obstruction_idx = i

        # Зона Френеля (60%)
        if distance_m > 0 and freq_mhz > 0:
            d1_km = (f * distance_m) / 1000.0
            d2_km = ((1 - f) * distance_m) / 1000.0
            f_ghz = freq_mhz / 1000.0
            fresnel_radius = 17.32 * math.sqrt((d1_km * d2_km) / (f_ghz * (distance_m / 1000.0)))
            clearance_required = 0.6 * fresnel_radius
            
            if terrain > (line_height - clearance_required):
                fresnel_clear = False

    # Розрахунок бюджету лінку (RSSI)
    d_km = max(0.01, distance_m / 1000.0)
    fspl = 20 * math.log10(d_km) + 20 * math.log10(freq_mhz) + 32.44
        
    diffraction_loss = 0.0
    if not los_clear and obstruction_idx > 0:
        f = obstruction_idx / (n - 1)
        d1 = f * distance_m
        d2 = (1 - f) * distance_m
        line_at_obs = elev_a + f * (elev_b - elev_a)
        h_obs = elevs[obstruction_idx] - line_at_obs
        diffraction_loss = calculate_diffraction_loss(h_obs, d1, d2, freq_mhz)

    total_path_loss = fspl + diffraction_loss + vegetation_loss

    # Створення профілів для графіку
    terrain_profile = [round(e, 1) for e in elevs]
    los_beam = []
    fresnel_60 = []

    for i in range(n):
        f = i / (n - 1)
        line_height = elev_a + f * (elev_b - elev_a)
        los_beam.append(round(line_height, 1))
        
        d1_km = (f * distance_m) / 1000.0
        d2_km = ((1 - f) * distance_m) / 1000.0
        fresnel_radius = 17.32 * math.sqrt((d1_km * d2_km) / ((freq_mhz/1000.0) * (distance_m/1000.0))) if distance_m > 0 else 0
        fresnel_60.append(round(line_height - 0.6 * fresnel_radius, 1))

    radio_horizon_km = 4.12 * (math.sqrt(base_height_a) + math.sqrt(base_height_b))
    is_beyond_horizon = (distance_m / 1000.0) > radio_horizon_km

    return {
        'пряма_видимість': los_clear and not is_beyond_horizon,  # Лояльніший критерій для mesh network
        'terrain_los': los_clear,
        'fresnel_los': fresnel_clear,
        'beyond_horizon': is_beyond_horizon,
        'radio_horizon_km': round(radio_horizon_km, 2),
        'статус': 'ВИДИМІСТЬ Є' if (los_clear and fresnel_clear and not is_beyond_horizon) else
                  ('ЗА ГОРИЗОНТОМ' if is_beyond_horizon else
                  (f"БЛОКОВАНО РЕЛЬЄФОМ (-{round(max_obstruction, 1)}м)" if not los_clear else "ЧАСТКОВЕ БЛОКУВАННЯ (ФРЕНЕЛЬ)")),
        'висота_початку_рельєф': round(elevs[0], 1),
        'висота_кінця_рельєф': round(elevs[-1], 1),
        'path_loss_db': round(total_path_loss, 1),
        'fspl_db': round(fspl, 1),
        'diffraction_loss_db': round(diffraction_loss, 1),
        'vegetation_loss_db': round(vegetation_loss, 1),
        'terrain_profile': terrain_profile,
        'los_beam': los_beam,
        'fresnel_60': fresnel_60,
        'необхідний_підйом_м': round(max_obstruction, 1) if not los_clear else 0.0,
        'відстань_м': round(distance_m, 1)
    }

def generate_synthetic_elevation(lat1, lng1, lat2, lng2, samples=30):
    """Генерує синтетичні дані рельєфу на основі координат (як fallback)."""
    rng = random.Random(int(abs(lat1 * 1000) + abs(lng1 * 1000) + abs(lat2 * 1000) + abs(lng2 * 1000)))
    base_elev = 200 + (lat1 - 48) * 50
    return [base_elev + rng.uniform(-5, 5) + math.sin(i/5)*10 for i in range(samples)]

# --- ROUTES ---

@app.route('/')
def index():
    # Для Render: HTML в корені репозиторію поруч з app.py
    easyk_path = Path(__file__).parent / "easykropyva_terminal_v1_5.html"
    if easyk_path.exists():
        return Response(easyk_path.read_text(encoding="utf-8"), mimetype="text/html")
    return "<h1>HTML file not found</h1>", 404

@app.route('/api/los', methods=['POST'])
def api_los():
    payload = request.get_json(silent=True) or {}
    lat1 = payload.get('lat1')
    lng1 = payload.get('lng1')
    lat2 = payload.get('lat2')
    lng2 = payload.get('lng2')

    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return jsonify({'помилка': 'Відсутні координати'}), 400

    base_h_a = payload.get('base_height_a', 0.5)
    base_h_b = payload.get('base_height_b', 0.5)
    samples = payload.get('samples', 30)
    freq_mhz = payload.get('freq_mhz', 433.0)

    elevations = None
    try:
        coords = sample_line(lat1, lng1, lat2, lng2, samples=samples)
        loc_str = '|'.join(f'{lat},{lng}' for lat, lng in coords)
        resp = requests.get(f'https://api.open-elevation.com/api/v1/lookup?locations={loc_str}', timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            elevations = [r['elevation'] for r in data.get('results', [])]
    except: pass

    if elevations is None:
        elevations = generate_synthetic_elevation(lat1, lng1, lat2, lng2, samples)

    dist_m = haversine_distance(lat1, lng1, lat2, lng2)
    result = compute_los(elevations, base_h_a, base_h_b, freq_mhz, dist_m)

    # Примусово додаємо дані профілю якщо їх немає
    if not result.get('terrain_profile'):
        result['terrain_profile'] = [round(e, 1) for e in elevations]
        result['los_beam'] = [round(elevations[0] + base_h_a + (elevations[-1] + base_h_b - elevations[0] - base_h_a) * i / len(elevations), 1) for i in range(len(elevations))]
        result['fresnel_60'] = [round(h - 5, 1) for h in result['los_beam']]

    return jsonify(result)

@app.route('/api/nodes', methods=['GET', 'POST', 'DELETE'])
def api_nodes():
    global nodes
    if request.method == 'GET':
        result = []
        for n in nodes.values():
            n_copy = dict(n)
            if n_copy.get('type') in ('sensor', 'relay'):
                n_copy.setdefault('battery', round(70 + (hash(str(n_copy.get('id', ''))) % 30), 0))
                n_copy.setdefault('signal_dbm', round(-80 + (hash(str(n_copy.get('id', '')) + 'sig') % 30), 1))
                n_copy.setdefault('online', True)
                n_copy.setdefault('mesh_active', n_copy.get('mesh_active', True))
                n_copy.setdefault('last_seen', time.time())
            result.append(n_copy)
        return jsonify(result)
    elif request.method == 'POST':
        data = request.get_json()
        node_id = int(time.time() * 1000)
        data['id'] = node_id
        nodes[node_id] = data
        return jsonify(data), 201
    elif request.method == 'DELETE':
        node_id = request.args.get('id', type=int)
        if node_id in nodes: del nodes[node_id]
        return jsonify({'ok': True})

@app.route('/api/targets', methods=['GET', 'POST', 'DELETE'])
def api_targets():
    global targets
    if request.method == 'GET':
        return jsonify(list(targets.values()))
    elif request.method == 'POST':
        data = request.get_json()
        target_id = int(time.time() * 1000)
        data['id'] = target_id
        targets[target_id] = data
        return jsonify(data), 201
    elif request.method == 'DELETE':
        target_id = request.args.get('id', type=int)
        if target_id in targets: del targets[target_id]
        return jsonify({'ok': True})

@app.route('/api/nodes/<int:node_id>', methods=['PATCH'])
def patch_node(node_id):
    global nodes
    if node_id not in nodes:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    nodes[node_id].update(data)
    return jsonify(nodes[node_id])

@app.route('/api/targets/<int:target_id>', methods=['PATCH'])
def patch_target(target_id):
    global targets
    if target_id not in targets:
        return jsonify({'error': 'Target not found'}), 404
    data = request.get_json(silent=True) or {}
    targets[target_id].update(data)
    return jsonify(targets[target_id])

@app.route('/api/mesh_topology')
def api_mesh_topology():
    try:
        mesh_nodes = [n for n in nodes.values() if n.get('type') in ['sensor', 'relay']]
        if len(mesh_nodes) < 2:
            return jsonify({'links': [], 'masters': [], 'routes': []})

        # Крок 1: збираємо всі пари та рахуємо LOS
        pairs = []
        for i in range(len(mesh_nodes)):
            for j in range(i + 1, len(mesh_nodes)):
                n1, n2 = mesh_nodes[i], mesh_nodes[j]
                dist = haversine_distance(n1['lat'], n1['lng'], n2['lat'], n2['lng'])
                if dist < 30000:
                    try:
                        elevs = generate_synthetic_elevation(n1['lat'], n1['lng'], n2['lat'], n2['lng'], 15)
                        freq = 900.0 if n1.get('model') == 'relay' or n2.get('model') == 'relay' else 433.0
                        res = compute_los(elevs, n1.get('altitude_m', 0.5), n2.get('altitude_m', 0.5), freq, dist)
                        pairs.append({
                            'source': n1['id'], 'target': n2['id'],
                            'distance_m': round(dist, 1),
                            'has_los': res['пряма_видимість'],
                            'terrain_los': res['terrain_los'],
                            'fresnel_los': res['fresnel_los'],
                            'beyond_horizon': res['beyond_horizon'],
                            'path_loss_db': res['path_loss_db']
                        })
                    except:
                        pass

        # Крок 2: будуємо граф — спочатку з LOS-з'єднань, потім підключаємо ізольовані
        graph = {}
        node_ids = [n['id'] for n in mesh_nodes]
        for nid in node_ids:
            graph[nid] = {}

        # Перший прохід: тільки LOS-з'єднання
        for p in pairs:
            if p['terrain_los'] and not p['beyond_horizon']:
                w = p['distance_m']
                graph[p['source']][p['target']] = w
                graph[p['target']][p['source']] = w
                p['link_type'] = 'direct_los'  # позначка для фронтенду

        # Другий прохід: ізольовані ноди — підключаємо через найближчу Fresnel або найкоротшу відстань
        # Сортуємо пари: Fresnel перші, потім найкоротші
        fallback_pairs = sorted(
            [p for p in pairs if p.get('link_type') != 'direct_los'],
            key=lambda x: (not x.get('fresnel_los', False), x['distance_m'])
        )

        for nid in node_ids:
            degree = len(graph[nid])
            if degree > 0:
                continue  # вже підключена

            # Ізольована — шукаємо найкращий fallback до БУДЬ-ЯКОЇ іншої ноди
            best = None
            for p in fallback_pairs:
                other = p['target'] if p['source'] == nid else (p['source'] if p['target'] == nid else None)
                if other is not None:
                    # Перевіряємо чи інша нода має хоч якийсь зв'язок (щоб не підключати ізольовану до ізольованої)
                    if len(graph[other]) > 0 or other == nid:
                        continue
                    best = p
                    break

            # Якщо інша теж ізольована — просто беремо найближчу пару
            if best is None:
                for p in fallback_pairs:
                    if p['source'] == nid or p['target'] == nid:
                        best = p
                        break

            if best:
                w = best['distance_m']
                graph[best['source']][best['target']] = w
                graph[best['target']][best['source']] = w
                best['link_type'] = 'fallback'

        # Крок 3: Дейкстра для кожної ноди — знаходимо найкоротші шляхи до всіх інших
        def dijkstra(start, graph, all_nodes):
            dist = {n: float('inf') for n in all_nodes}
            prev = {n: None for n in all_nodes}
            dist[start] = 0
            unvisited = set(all_nodes)

            while unvisited:
                u = min(unvisited, key=lambda x: dist[x])
                unvisited.remove(u)
                if dist[u] == float('inf'):
                    break
                for v, w in graph.get(u, {}).items():
                    if v in unvisited:
                        alt = dist[u] + w
                        if alt < dist[v]:
                            dist[v] = alt
                            prev[v] = u
            return prev, dist

        # Крок 4: збираємо всі ребра, що входять в реальні маршрути
        used_edges = set()  # frozenset([a, b])
        routes = []         # список маршрутів для інфи

        for src in node_ids:
            prev, dist = dijkstra(src, graph, node_ids)
            for dst in node_ids:
                if src >= dst:  # кожну пару один раз (симетрично)
                    continue
                if dist[dst] == float('inf'):
                    continue  # немає шляху

                # Відновлюємо шлях
                path = []
                cur = dst
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                path.reverse()

                if len(path) >= 2:
                    # Додаємо всі ребра шляху
                    for k in range(len(path) - 1):
                        edge = frozenset([path[k], path[k+1]])
                        used_edges.add(edge)

                    routes.append({
                        'source': src,
                        'target': dst,
                        'hops': len(path) - 1,
                        'path': path,
                        'distance_m': round(dist[dst], 1)
                    })

        # Крок 5: формуємо фінальні лінки з used_edges
        pair_map = {}
        for p in pairs:
            key = frozenset([p['source'], p['target']])
            pair_map[key] = p

        final_links = []
        for edge in used_edges:
            a, b = tuple(edge)
            if edge in pair_map:
                p = pair_map[edge]
                final_links.append({
                    'source': p['source'],
                    'target': p['target'],
                    'distance_m': p['distance_m'],
                    'has_los': p['has_los'],
                    'terrain_los': p['terrain_los'],
                    'fresnel_los': p['fresnel_los'],
                    'beyond_horizon': p['beyond_horizon'],
                    'link_type': p.get('link_type', 'direct_los')
                })

        # Крок 6: майстер-ноди — ті, через які проходить найбільше маршрутів
        route_through = {nid: 0 for nid in node_ids}
        hop_count = {}  # hops від source до кожного target
        for r in routes:
            for hop_node in r['path'][1:-1]:
                route_through[hop_node] = route_through.get(hop_node, 0) + 1
            key = (r['source'], r['target'])
            hop_count[key] = r['hops']

        masters = []
        if route_through:
            max_val = max(route_through.values())
            for nid, count in route_through.items():
                if count > 0 and count >= max_val * 0.5:
                    masters.append(nid)

        # Додаємо hop_count та route_through до лінків
        for link in final_links:
            key = (link['source'], link['target'])
            link['hops'] = hop_count.get(key, 1)
            # Симуляція енерго-статусу на основі battery
            src_node = nodes.get(link['source'], {})
            battery = src_node.get('battery', 100)
            if battery < 20:
                link['energy_state'] = 'hibernate'
            elif battery < 50:
                link['energy_state'] = 'sleep'
            elif battery < 80:
                link['energy_state'] = 'verify'
            else:
                link['energy_state'] = 'alert'

        return jsonify({
            'links': final_links,
            'masters': masters,
            'routes': routes,
            'route_through': route_through
        })

    except Exception as e:
        print(f"Mesh topology error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'links': [], 'masters': [], 'routes': [], 'error': str(e)}), 500

@app.route('/api/home', methods=['GET', 'POST', 'DELETE'])
def api_home_point():
    global home_point
    if request.method == 'GET':
        return jsonify(home_point or {})
    elif request.method == 'DELETE':
        home_point = None
        return jsonify({'ok': True})
    home_point = request.get_json()
    return jsonify({'ok': True})

@app.route('/api/clear', methods=['POST'])
def api_clear():
    global nodes, targets
    nodes.clear()
    targets.clear()
    return jsonify({'ok': True})

@app.route('/api/test_terrain', methods=['GET'])
def api_test_terrain():
    """Тестовий endpoint для перевірки оновлення бекенду"""
    return jsonify({
        'status': 'backend_updated',
        'version': 'v1.6.3',
        'terrain_profile': [200, 205, 203, 210, 208, 215, 212, 218],
        'los_beam': [200.5, 205.3, 203.2, 210.4, 208.1, 215.2, 212.3, 218.1],
        'fresnel_60': [195.5, 200.3, 198.2, 205.4, 203.1, 210.2, 207.3, 213.1]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)