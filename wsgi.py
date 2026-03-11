import sys
import os

# Add your project directory to the sys.path
project_home = '/home/YOUR_USERNAME/finance-dashboard'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Set the working directory
os.chdir(project_home)

from app import app as application
