import sys
import time
import requests
from PyQt5 import QtWidgets, QtGui, QtCore
from google.transit import gtfs_realtime_pb2

# Replace with your actual API key
API_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJyVDRub1k4NW5HSkxIbkFXRDN4MnJza1VxZ3QzemtUbGxXV1hBWWR0RDVvIiwiaWF0IjoxNzQ2Nzc3MDU2fQ.jqBsD4F3FAOEtVnquL-iJKOYqct10PNk-AQ3Gh2MUDI"
HEADERS = {"Authorization": f"apikey {API_KEY}"}

BUS_STOP_ID = "200923"  # Union Square, Harris Street
REALTIME_URL = "https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses"
VEHICLE_POSITIONS_URL = "https://api.transport.nsw.gov.au/v1/gtfs/vehiclepos/buses"
ROUTES_URL = "https://api.transport.nsw.gov.au/v1/routes"

class BusArrival:
    def __init__(self, route_id, route_name, scheduled_arrival_time, actual_arrival_time, vehicle_pos):
        self.route_id = route_id
        self.route_name = route_name
        self.scheduled_arrival_time = scheduled_arrival_time
        self.actual_arrival_time = actual_arrival_time
        self.vehicle_pos = vehicle_pos

    def time_until(self):
        now = int(time.time())
        return max(0, (self.actual_arrival_time - now) // 60)

    def delay_str(self):
        delay_sec = self.actual_arrival_time - self.scheduled_arrival_time
        if delay_sec == 0:
            return "On time", "green"
        elif delay_sec > 0:
            return f"{delay_sec // 60} minutes late", "red"
        else:
            return f"{abs(delay_sec) // 60} minutes early", "blue"

class BusDisplayApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Union Square Bus Arrivals")
        self.setStyleSheet("background-color: #121212; color: white;")
        self.setGeometry(300, 300, 500, 300)

        layout = QtWidgets.QVBoxLayout()
        self.bus_list = QtWidgets.QListWidget()
        self.bus_list.setStyleSheet("font-size: 16px;")

        layout.addWidget(self.bus_list)
        self.setLayout(layout)

        self.update_buses()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_buses)
        self.timer.start(15000)  # update every 15 seconds

    def update_buses(self):
        self.bus_list.clear()
        try:
            buses = fetch_bus_data()
            if not buses:
                raise ValueError("No arrivals found for this stop.")
            for bus in buses[:3]:
                mins = bus.time_until()
                delay_text, color = bus.delay_str()
                if mins == 0:
                    mins = "Now"
                item = QtWidgets.QListWidgetItem()
                item.setText(f"{bus.route_name}\t{mins} min\t{delay_text}\nAt {bus.vehicle_pos}")
                item.setForeground(QtGui.QColor(color))
                self.bus_list.addItem(item)
        except Exception as e:
            error_item = QtWidgets.QListWidgetItem(f"Error fetching data: {str(e)}")
            error_item.setForeground(QtGui.QColor("red"))
            self.bus_list.addItem(error_item)

def fetch_bus_data():
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        resp = requests.get(REALTIME_URL, headers=HEADERS)
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch trip data: {resp.status_code}")
        feed.ParseFromString(resp.content)
    except Exception as e:
        print(f"Error fetching trip updates: {e}")
        return []

    vehicle_feed = gtfs_realtime_pb2.FeedMessage()
    try:
        vehicle_resp = requests.get(VEHICLE_POSITIONS_URL, headers=HEADERS)
        if vehicle_resp.status_code != 200:
            raise ValueError(f"Failed to fetch vehicle data: {vehicle_resp.status_code}")
        vehicle_feed.ParseFromString(vehicle_resp.content)
    except Exception as e:
        print(f"Error fetching vehicle positions: {e}")
        return []

    vehicle_positions = {v.vehicle.vehicle.id: v.vehicle.position for v in vehicle_feed.entity if v.HasField("vehicle")}

    # Fetch route names from the Routes API
    route_names = fetch_route_names()

    buses = []
    for entity in feed.entity:
        try:
            if not entity.HasField("trip_update"):
                continue

            trip = entity.trip_update
            stop_time_updates = [stu for stu in trip.stop_time_update if stu.stop_id == BUS_STOP_ID]
            if not stop_time_updates:
                continue

            stu = stop_time_updates[0]
            scheduled_arrival = stu.arrival.time if stu.HasField("arrival") else 0
            actual_arrival = stu.arrival.time if stu.HasField("arrival") else 0
            route_id = trip.trip.route_id
            vehicle_id = trip.vehicle.id if trip.HasField("vehicle") else None

            vehicle_pos = "Unknown location"
            if vehicle_id and vehicle_id in vehicle_positions:
                pos = vehicle_positions[vehicle_id]
                vehicle_pos = f"Lat: {pos.latitude:.3f}, Lon: {pos.longitude:.3f}"

            route_name = route_names.get(route_id, "Unknown Route")

            buses.append(BusArrival(route_id, route_name, scheduled_arrival, actual_arrival, vehicle_pos))
        except Exception as e:
            print(f"Error parsing entity: {e}")

    buses.sort(key=lambda b: b.actual_arrival_time)
    return buses

def fetch_route_names():
    route_names = {}
    try:
        response = requests.get(ROUTES_URL, headers=HEADERS)
        if response.status_code == 200:
            routes = response.json()
            for route in routes:
                route_names[route['route_id']] = route['MY_TIMETABLE_ROUTE_NAME']

        if response.status_code == 400:
            print(f"uh oh, thats a 400")

        else:
            print(f"Failed to fetch route names: {response.status_code}")
    except Exception as e:
        print(f"Error fetching route names: {e}")
    return route_names

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    viewer = BusDisplayApp()
    viewer.show()
    sys.exit(app.exec_())
