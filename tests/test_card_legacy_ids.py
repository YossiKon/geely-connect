"""The cards read the entity ids an entry migration keeps from older versions.

The migration moves those rows to their current unique ids in place, so each
keeps its old entity id - and its history. A card looking only for the current
spelling showed no odometer and no doors on such an install.
"""
from test_cards_in_a_browser import _evaluate


def _run(body):
    return _evaluate("""() => {
      const el = document.createElement('geely-card');
      document.body.appendChild(el);
      el.setConfig({prefix: 'car'});
      const hass = window.mkHass({});
      """ + body + """
    }""")


_OLD_IDS = """{
  'sensor.total_mileage': 'sensor.car_odometer',
  'binary_sensor.door_driver': 'binary_sensor.car_driver_door_open',
  'binary_sensor.door_passenger': 'binary_sensor.car_passenger_door_open',
  'binary_sensor.door_rear_left': 'binary_sensor.car_rear_left_door_open',
  'binary_sensor.door_rear_right': 'binary_sensor.car_rear_right_door_open'
}"""


def test_odometer_and_door_ids_kept_from_older_versions_are_read():
    got = _run("""
      const old = """ + _OLD_IDS + """;
      for (const [now, id] of Object.entries(old)) {
        const current = now.replace('.', '.car_');
        hass.states[id] = {state: now.startsWith('sensor.') ? '12345' : 'on', attributes: {}};
        hass.entities[id] = {platform: 'geely_connect', device_id: 'd'};
        delete hass.states[current]; delete hass.entities[current];
      }
      el.hass = hass;
      return Object.keys(old).map((key) => (el._st(key) || {}).state ?? null);
    """)
    assert got == ["12345", "on", "on", "on", "on"], got


def test_an_old_id_names_the_car_but_never_reads_another_cars_entity():
    got = _run("""
      hass.states = {
        'sensor.car_odometer': {state: '12345', attributes: {}},
        'binary_sensor.other_driver_door_open': {state: 'on', attributes: {}},
      };
      hass.entities = {
        'sensor.car_odometer': {platform: 'geely_connect', device_id: 'd'},
        'binary_sensor.other_driver_door_open': {platform: 'geely_connect', device_id: 'other'},
      };
      el.hass = hass;
      return {mileage: (el._st('sensor.total_mileage') || {}).state ?? null,
              door: (el._st('binary_sensor.door_driver') || {}).state ?? null};
    """)
    assert got == {"mileage": "12345", "door": None}, got


def test_the_current_spelling_wins_over_a_kept_old_one():
    # The current door sensor is renamed, so the card finds it through its
    # map rather than by its id - where the kept old one could take its place.
    got = _run("""
      const cur = 'binary_sensor.car_door_driver', renamed = 'binary_sensor.front_door_driver';
      hass.states[renamed] = {state: 'off', attributes: {}};
      const device = hass.entities['sensor.car_battery'].device_id;
      hass.entities[renamed] = {platform: 'geely_connect', device_id: device};
      delete hass.states[cur]; delete hass.entities[cur];
      hass.states['binary_sensor.old_driver_door_open'] = {state: 'on', attributes: {}};
      hass.entities = {'binary_sensor.old_driver_door_open':
                         {platform: 'geely_connect', device_id: device}, ...hass.entities};
      el.hass = hass;
      return el._st('binary_sensor.door_driver').state;
    """)
    assert got == "off", got


def _two_cars(prefix):
    return _evaluate("""() => {
      const el = document.createElement('geely-card');
      document.body.appendChild(el);
      el.setConfig({prefix: '""" + prefix + """'});
      const hass = window.mkHass({});
      const put = (id, state, device) => {
        hass.states[id] = {state, attributes: {}};
        hass.entities[id] = {platform: 'geely_connect', device_id: device};
      };
      hass.states = {}; hass.entities = {};
      // This car's battery is renamed and the other car's slug begins with
      // the prefix too: only the kept odometer names this one exactly.
      put('sensor.pack_battery', '61', 'd');
      put('sensor.car_odometer', '12345', 'd');
      put('sensor.car_two_battery', '90', 'o');
      put('sensor.car_two_odometer', '999', 'o');
      el.hass = hass;
      return {lost: !!el._lostCar(),
              battery: (el._st('sensor.battery') || {}).state ?? null,
              mileage: (el._st('sensor.total_mileage') || {}).state ?? null};
    }""")


def test_a_kept_old_id_alone_names_its_car_among_several():
    got = _two_cars("car")
    assert got == {"lost": False, "battery": "61", "mileage": "12345"}, got


def test_a_prefix_that_names_no_car_reads_no_old_id_either():
    got = _two_cars("nope")
    assert got == {"lost": True, "battery": None, "mileage": None}, got
