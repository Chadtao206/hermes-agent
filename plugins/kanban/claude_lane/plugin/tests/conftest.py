import os
import sys

# Put the plugin package dir on sys.path so tests can `import router`, etc.
# (Under the real gateway the loader imports it as a package; tests run it flat.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
