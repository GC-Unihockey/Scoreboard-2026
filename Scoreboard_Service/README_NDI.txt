NDI Output (Linux)
------------------
This project uses outputs/ndi_scoreboard.py which sends NDI video using the official NDI runtime (libndi) via ctypes.

Requirements (Ubuntu):
  sudo apt install -y python3-numpy python3-opencv

Verify NDI runtime:
  python3 -c "import ctypes; ctypes.CDLL('libndi.so'); print('libndi ok')"

Enable NDI:
  Set ENABLE_NDI = True in main_simulator.py or main_live.py

