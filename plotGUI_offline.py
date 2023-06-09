import signal
import os.path
import dash
from dash import dcc
from dash import html
from dash.dependencies import Input, Output, State
import dash_daq as daq
from plotly.subplots import make_subplots
import plotly.graph_objects as go
import time
import serial
import threading
import datetime
import logging


# Config file variables
SCAN_COM_PORTS = None # True/False
MAX_DATA_TIME = None # minutes
REFRESH_RATE = None # seconds
COM_PORT = None # 'COM#' or None
path = os.path.dirname(os.path.abspath(__file__))  # path to this file

# If a config file exists, read and set the values from it
# If not, create a config file with default values
if os.path.isfile(str(path + "/config.txt")):
    print("Config file found")
    with open(str(path + "/config.txt"), "r") as f:
        for line in f:
            line = line.rstrip()
            data = line.split("=")

            if data[0] == "SCAN_COM_PORTS":
                if data[1] == "True":
                    SCAN_COM_PORTS = True

            elif data[0] == "MAX_DATA_TIME":
                print("\tSetting MAX_DATA_TIME to " + str(data[1]) + " minutes")
                MAX_DATA_TIME = int(data[1])

            elif data[0] == "REFRESH_RATE":
                print("\tSetting REFRESH_RATE to " + str(data[1]) + " second(s)")
                REFRESH_RATE = int(data[1])

            # Only set COM_PORT if SCAN_COM_PORTS is False and COM_PORT is not None
            # Otherwise, default COM_PORT to 3
            elif data[0] == "COM_PORT":
                if not SCAN_COM_PORTS and str(data[1]) != "None":
                    print("\tSetting COM_PORT to " + str(data[1]))
                    COM_PORT = "COM" + str(data[1])
                else:
                    print("\tNo COM_PORT set and SCAN_COM_PORTS is false. Defaulting COM_PORT to 3")
                    COM_PORT = "COM3"
else:
    print("Config file not found. Creating config file...")

    # Default config values
    SCAN_COM_PORTS = True
    MAX_DATA_TIME = 120
    REFRESH_RATE = 2

    with open(str(path + "/config.txt"), "w") as f:
        f.write("SCAN_COM_PORTS=" + str(SCAN_COM_PORTS) + "\n")
        f.write("MAX_DATA_TIME=" + str(MAX_DATA_TIME) + "\n")
        f.write("REFRESH_RATE=" + str(REFRESH_RATE) + "\n")
        f.write("COM_PORT=" + str(COM_PORT))
print("Config file loaded")


# Global variables
activeThreads = [] # list of active threads
serialReadWrite = None # serial port object
plotData = [[], [], [], []] # list of lists of data for each plot
threadsPaused = False # flag to pause threads
stopThreads = False # flag to stop threads
preventCallback = False # flag to prevent callbacks from running
currentValues = {
    "temp": 0,
    "ph": 0,
    "dissolved_oxygen": 0
}
buttonStates = {
    "pump": "0",
    "aerator": "0",
    "lights": "0",
    "plug4": "0",
    "stop": "0",
}
log = logging.getLogger('werkzeug') # Flask/Dash logger
log.setLevel(logging.ERROR) # suppress Flask/Dash logging unless it's an error

# Initialize serial port
# Scan COM ports until one is found
if SCAN_COM_PORTS:
    for i in range(32):
        time.sleep(0.25) # helps to prevent missed scans
        try:
            serialReadWrite = serial.Serial("COM" + str(i), 9600, timeout=1)
            print("COM" + str(i) + ": connected")
            COM_PORT = "COM" + str(i)
            break
        except:
            print("COM" + str(i) + ": not connected")
            pass
        if i == 31:
            print("No serial port available. Exiting...")
            exit()
else:
    try:
        serialReadWrite = serial.Serial(COM_PORT, 9600, timeout=1)
        print(COM_PORT + ": connected")
    except:
        print(COM_PORT + ": not connected.\n" + 
            "Please set COM_PORT in config.txt to a valid COM port or set SCAN_COM_PORTS to True.\n" +
              "Exiting...")
        exit()



# Initialize button states
def initializeButtonStates():
    print("Initializing button states from Arduino...")
    try:
        parseSerialData()
        print("Button states initialized")
    except:
        pass


# Signal Handlers
# Handle Ctrl+C
def signalHandler(sig, frame):
    global stopThreads
    global threadsPaused

    print("Exiting...")

    # Kill all threads
    stopThreads = True
    activeThreads = threading.enumerate()
    for thread in activeThreads:
        if thread.name != "MainThread":
            print("Stopping thread: " + thread.name)
            thread.join()

    exit()

signal.signal(signal.SIGINT, signalHandler)
signal.signal(signal.SIGTERM, signalHandler)



# Thread handlers
def startThread(thread):
    if thread.is_alive():
        print("Thread already running: " + thread.name)
    else:
        print("Started thread: " + thread.name)
        thread.start()

def resumeThreads():
    global threadsPaused
    threadsPaused = False

def pauseThreads():
    global threadsPaused
    threadsPaused = True



# Handle application exit
def handleExit():
    global stopThreads

    print("Exiting...")

    # Kill all threads
    stopThreads = True
    activeThreads = threading.enumerate()
    for thread in activeThreads:
        if thread.name != "MainThread":
            print("Stopping thread: " + thread.name)
            thread.join()

    exit()



# Parse data from the current COM port
def parseSerialData():
    global serialReadWrite

    if serialReadWrite.in_waiting > 0:
        data = serialReadWrite.readline()
        
        try:
            data = data.decode("utf-8")
        except:
            print("Error decoding data: " + str(data) + "\nRetrying...")
            return parseSerialData()

        data = data.rstrip()
        data = data.replace(" ", "")
        data = data.split(",")

        # ['temp', 'ph', 'DO', 'button1', 'button2', 'button3', 'button4', 'STOP']
        if data[0] == "ResetButtonPressed":
            print("Reset button pressed -- waiting for reboot...")
            return parseSerialData()
        elif data[0].startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
            if len(data) != 8:
                print("Received invalid data format: " + str(data) + "\nRetrying...")
                return parseSerialData()
            else:
                return data
        


# Read each line of data in the file
# Compare the current time with the timestamp
# If the timestamp is older than MAX_DATA_TIME, delete the line
# Write the data back to the file
def cleanFileData():
    print("Cleaning stale data from file...")
    with open(str(path + "/data.csv"), "r") as input:
        with open(str(path + "/temp.csv"), "w") as output:
            for line in input:
                line = line.rstrip()
                data = line.split(",")

                # Only keep data that is less than MAX_DATA_TIME old, 
                # and that has a valid data values
                if int(time.time()) - int(data[0]) < (MAX_DATA_TIME * 60) and validateFileData(data) == True:
                    output.write(line + "\n")
    os.replace(str(path + "/temp.csv"), str(path + "/data.csv"))
    print("Finished cleaning data from file")

def validateFileData(data):
    if len(data) != 9:
        print("Removing invalid data from file:\n" + "\t" + str(data))
        return False
    
    # Reject data that is not an int or float
    for i in range(1, 8):
        try:
            data[i] = float(data[i])
        except:
            print("Removing invalid data from file:\n" + "\t" + str(data))
            return False
        try:
            data[i] = int(data[i])
        except:
            print("Removing invalid data from file:\n" + "\t" + str(data))
            return False
        
    return True



# Read the data file and send to handlePlotData
def readFileData():
    print("Reading existing data from file...")
    with open(str(path + "/data.csv"), "r") as f:
        for line in f:
            line = line.rstrip()
            data = line.split(",")
            handlePlotData(data)
    print("Finished reading data from file")



# Write data to file
# Send data to handlePlotData
def writeFileData():
    global serialReadWrite

    # Initially read the data file, if it exists
    if os.path.isfile(str(path + "/data.csv")):
        cleanFileData()
        readFileData()

    print("Waiting for incoming data...")
    while True:
        global stopThreads
        global threadsPaused
        if stopThreads:
            break
        if threadsPaused:
            time.sleep(0.1)
            continue

        # Try to grab data from the serial port
        # If there is an error, wait 10 seconds and try reconnecting
        # The device was most likely unplugged and needs to be reconnected
        try:
            data = parseSerialData()
        except:
            print("Error parsing serial data. Ensure the device is plugged in.\nRetrying " + COM_PORT + " in 10 seconds...")
            time.sleep(10)
            try:
                serialReadWrite = serial.Serial(COM_PORT, 9600, timeout=1)
                print(COM_PORT + ": connected. Resuming...")
                continue
            except:
                print(COM_PORT + ": not connected. Retrying...")
                pass
            continue

        if data:
            data.insert(0, str(int(time.time())))  # add timestamp
            print("+ file write: " + str(data))
            with open(str(path + "/data.csv"), "a") as f:
                f.write(",".join(data) + "\n")
            handleButtonData(data)
            handlePlotData(data)
            time.sleep(0.1)
        else:
            pass

# Handle incoming button states data
def handleButtonData(data):
    global buttonStates

    if data:
        # Update buttonStates

        if buttonStates["pump"] != data[4]:
            buttonStates["pump"] = data[4]
            if data[4] == "1":
                print("Pump turned on")
            else:
                print("Pump turned off")
        if buttonStates["aerator"] != data[5]:
            buttonStates["aerator"] = data[5]
            if data[5] == "1":
                print("Aerator turned on")
            else:
                print("Aerator turned off")
        if buttonStates["lights"] != data[6]:
            buttonStates["lights"] = data[6]
            if data[6] == "1":
                print("Lights turned on")
            else:
                print("Lights turned off")
        if buttonStates["plug4"] != data[7]:
            buttonStates["plug4"] = data[7]
            if data[7] == "1":
                print("Plug 4 turned on")
            else:
                print("Plug 4 turned off")

    return buttonStates

# Handle incoming data
def handlePlotData(data):
    global plotData

    if data:
        # Convert epoch to datetime
        data[0] = datetime.datetime.fromtimestamp(int(data[0]))

        # Update currentValues
        currentValues["temp"] = data[1]
        currentValues["ph"] = data[2]
        currentValues["dissolved_oxygen"] = data[3]

        # Update plotData
        try:
            plotData[0].append(data[0]) # timestamp
            plotData[1].append(float(data[1]))  # temperature
            plotData[2].append(float(data[2]))  # pH
            plotData[3].append(float(data[3]))  # dissolved oxygen
        except:
            print("\nError parsing data from file: \n\t" + str(data) + "\n\tSkipping data entry\n")
            return plotData
        
    # Loop through plotData[0] (timestamps) and remove any data older than MAX_DATA_TIME
    # If we reach a timestamp that is less than MAX_DATA_TIME old, break the loop
    for i in range(len(plotData[0])):
        if int(time.time()) - int(plotData[0][0].timestamp()) > (MAX_DATA_TIME * 60):
            plotData[0].pop(0) # timestamp
            plotData[1].pop(0) # temperature
            plotData[2].pop(0) # pH
            plotData[3].pop(0) # dissolved oxygen
        else:
            break

    return plotData



# Every MAX_DATA_TIME minutes, run the cleanFileData function.
# This polling method of waiting is not CPU efficient,
# but this program is not CPU intensive and it is the easiest way to do this
def handleClean():
    global stopThreads
    global threadsPaused
    while True:
        if stopThreads:
            break

        for i in range(MAX_DATA_TIME * 60):
            if stopThreads:
                break
            time.sleep(1)
        threadsPaused = True
        cleanFileData()
        threadsPaused = False



# Is called when a toggle button is pressed
# Writes the current button states to serial
def writeButtonStates():
    global serialReadWrite
    global buttonStates

    # Separate the button states with commas
    stateMessage = ""
    for button in buttonStates:
        stateMessage += str(buttonStates[button]) + ","
    stateMessage = stateMessage[:-1] # Remove the last comma
    print("Sending button states to serial: " + stateMessage)
    stateMessage += "\n" # Add a newline character

    return serialReadWrite.write(bytes(stateMessage, "utf-8"))



# Handle Dash window, UI elements
# Button for setting file path
def handleDashPlot():
    global plotData
    global stopThreads
    global threadsPaused

    initializeButtonStates()

    # Initialize Dash app
    app = dash.Dash(__name__)
    app.title = "Data Plotter"
    app.css.config.serve_locally = True
    app.scripts.config.serve_locally = True

    # Set Dash layout
    app.layout = html.Div(id="main", children=[
        dcc.Store(id="button-states", storage_type="session", data=buttonStates),

        html.Div(id="data-display", children=[
            daq.LEDDisplay(id="temperature-display", label="Temperature (F)", value=currentValues["temp"], size=50, color="#777777", labelPosition="bottom"),
            daq.LEDDisplay(id="ph-display", label="pH Level", value=currentValues["ph"], size=50, color="#777777", labelPosition="bottom"),
            daq.LEDDisplay(id="dissolved-oxygen-display", label="Dissolved Oxygen (mg/L)", value=currentValues["dissolved_oxygen"], size=50, color="#777777", labelPosition="bottom"),
        ]),

        dcc.Graph(id="live-graph", animate=False),

        html.Div(id="toggle-button-group", children=[
            daq.BooleanSwitch(id="toggle-pump", on=int(buttonStates["pump"]), label="Pump", labelPosition="bottom", persistence=True, persistence_type="session"),
            daq.BooleanSwitch(id="toggle-aerator", on=int(buttonStates["aerator"]), label="Aerator", labelPosition="bottom", persistence=True, persistence_type="session"),
            daq.BooleanSwitch(id="toggle-lights", on=int(buttonStates["lights"]), label="Lights", labelPosition="bottom", persistence=True, persistence_type="session"),
            daq.BooleanSwitch(id="toggle-plug4", on=int(buttonStates["plug4"]), label="Plug 4", labelPosition="bottom", persistence=True, persistence_type="session"),
            daq.StopButton(id="stop-button", n_clicks=0),
        ]),

        dcc.Interval(
            id="graph-update",
            interval=REFRESH_RATE * 1000,
            n_intervals=0
        ),
    ])
            

    
    # TOGGLE BUTTON CALLBACKS
    # Update buttonStates when a toggle button is pressed
    @app.callback(
        Output("button-states", "data"),
        [Input("toggle-pump", "on"),
        Input("toggle-aerator", "on"),
        Input("toggle-lights", "on"),
        Input("toggle-plug4", "on")],
        [State("button-states", "data")],
        prevent_initial_call=True
    )
    def updateButtonStates(pump, aerator, lights, plug4, state):
        global buttonStates
        global preventCallback

        if preventCallback:
            preventCallback = False
            return state

        # If the stop button is pressed, set all button states to 0
        buttonStates["pump"] = "1" if pump else "0"
        buttonStates["aerator"] = "1" if aerator else "0"
        buttonStates["lights"] = "1" if lights else "0"
        buttonStates["plug4"] = "1" if plug4 else "0"

        writeButtonStates() # Write the button states to serial

        state = buttonStates

        return state
    
    # Update toggle buttons when buttonStates is updated
    # Refresh every REFRESH_RATE seconds
    @app.callback(
        [Output("toggle-pump", "on"),
        Output("toggle-aerator", "on"),
        Output("toggle-lights", "on"),
        Output("toggle-plug4", "on")],
        [Input("graph-update", "n_intervals")],
        [State("button-states", "data")],
        prevent_initial_call=True
    )
    def updateToggleButtons(n, state):
        global preventCallback

        preventCallback = True # Prevent infinite loop

        for button in buttonStates:
            if buttonStates[button] == "1":
                state[button] = True
            else:
                state[button] = False

        return state["pump"], state["aerator"], state["lights"], state["plug4"]
    
    # Send button states to serial when the stop button is pressed
    @app.callback(
        Output("stop-button", "n_clicks"),
        [Input("stop-button", "n_clicks")],
        prevent_initial_call=True
    )
    def stopButton(n_clicks):
        global buttonStates

        print("STOP requested\nSending STOP request to serial")
        buttonStates["stop"] = "1"
        writeButtonStates()
        buttonStates["stop"] = "0"

        return 0

    # DATA DISPLAY CALLBACKS
    # Update temperature data display
    @app.callback(
        Output("temperature-display", "value"),
        [Input("graph-update", "n_intervals")]
    )
    def updateTemperature(n):
        global currentValues
        return currentValues["temp"]
    
    # Update pH data display
    @app.callback(
        Output("ph-display", "value"),
        [Input("graph-update", "n_intervals")]
    )
    def updatePH(n):
        global currentValues
        return currentValues["ph"]
    
    # Update dissolved oxygen data display
    @app.callback(
        Output("dissolved-oxygen-display", "value"),
        [Input("graph-update", "n_intervals")]
    )
    def updateDissolvedOxygen(n):
        global currentValues
        return currentValues["dissolved_oxygen"]
    

            
    # Update graph
    @app.callback(
        Output("live-graph", "figure"),
        [Input("graph-update", "n_intervals")]
    )
    def updateGraph(n):
        global plotData

        # Create figure
        fig = go.Figure()

        # Setup subplot grid
        fig = make_subplots(
            rows=3,
            cols=1,
            shared_xaxes=True,
            row_heights=[1, 1, 1],
        )

        # Add traces
        # Each scatter plot uses WebGL for faster rendering
        fig.append_trace(go.Scattergl(
            x=plotData[0], 
            y=plotData[1],
            name="Temperature", 
            mode="lines+markers",
            marker=dict(
                size=5,
                color="#2ca25f"
                # color="rgba(44, 162, 95, 0.5)"
            ),
            line=dict(
                color="#2ca25f",
                width=3
            )
        ), row=1, col=1)
        fig.append_trace(go.Scattergl(
            x=plotData[0],
            y=plotData[2],
            name="pH",
            mode="lines+markers",
            marker=dict(
                size=5,
                color="#e34a33"
                # color="rgba(227, 74, 51, 0.5)",
            ),
            line=dict(
                color="#e34a33",
                width=3
            )
        ), row=2, col=1)
        fig.append_trace(go.Scattergl(
            x=plotData[0],
            y=plotData[3],
            name="Dissolved Oxygen",
            mode="lines+markers",
            marker=dict(
                size=5,
                color="#c51b8a"
                # color="rgba(197, 27, 138, 0.5)",
            ),
            line=dict(
                color="#c51b8a",
                width=3
            )
        ), row=3, col=1)

        # Figure layout
        fig.update_layout(
            showlegend=False,
            autosize=True,
            margin=dict(
                l=85,
                r=25,
                b=50,
                t=50,
                pad=4
            ),
            paper_bgcolor="#135EAB",
            font=dict(
                family="Lato",
                size=14,
                color="#ffffff"
            ),
            xaxis=dict(
                gridcolor="#D7D7D7",
                gridwidth=2
            ),
            xaxis2=dict(
                gridcolor="#D7D7D7",
                gridwidth=2
            ),
            xaxis3=dict(
                gridcolor="#D7D7D7",
                gridwidth=2
            ),
            yaxis=dict(
                title="Temperature (F)",
                gridcolor="#D7D7D7",
                gridwidth=2
            ),
            yaxis2=dict(
                title="Acidity (pH)",
                gridcolor="#D7D7D7",
                gridwidth=2
            ),
            yaxis3=dict(
                title="Dissolved Oxygen (mg/L)",
                gridcolor="#D7D7D7",
                gridwidth=2
            ),
            uirevision="true" # Maintains UI state through updates
        )

        return fig
    
    # Start threads
    startThread(writeThread)
    startThread(cleanThread)

    # Run Dash app
    app.run_server(debug=False, use_reloader=False)



# Main
writeThread = threading.Thread(target=writeFileData, name="writeThread")
cleanThread = threading.Thread(target=handleClean, name="cleanThread")
print("Initialized threads\nInitializing Dash app...")
handleDashPlot()