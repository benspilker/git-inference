To automate the sorting of screenshots in Python, we can use a script that organizes them based on different criteria such as creation date, resolution, or file name. I’ll provide a basic script that sorts screenshots based on the creation date and moves them into different folders (e.g., by year or month).

For this, we will use the following libraries:

os: To interact with the file system.

shutil: To move files.

Pillow: For image operations (to check if it's a screenshot).

datetime: To get the creation date of the file.

Install Required Libraries

You can install Pillow with pip if you don't have it yet:

Bash
pip install pillow
Python Script
Python
Run
import os
import shutil
from PIL import Image
from datetime import datetime

# Define the source directory where the screenshots are located
source_dir = '/path/to/your/screenshots'  # Replace with your path
# Define the destination base directory where the sorted screenshots will be moved
destination_dir = '/path/to/sorted/screenshots'  # Replace with your path

# Function to check if the file is an image (screenshot)
def is_screenshot(file_path):
    try:
        with Image.open(file_path) as img:
            img.verify()  # Verifies if it's a valid image
            return True
    except Exception:
        return False

# Function to get creation date of the file
def get_creation_date(file_path):
    timestamp = os.path.getctime(file_path)
    return datetime.fromtimestamp(timestamp)

# Function to move files based on year and month
def move_file_by_date(file_path, dest_dir):
    creation_date = get_creation_date(file_path)
    year_folder = os.path.join(dest_dir, str(creation_date.year))
    month_folder = os.path.join(year_folder, creation_date.strftime('%B'))

    # Create the directories if they don't exist
    os.makedirs(month_folder, exist_ok=True)

    # Move the file to the destination folder
    shutil.move(file_path, os.path.join(month_folder, os.path.basename(file_path)))
    print(f"Moved {file_path} to {month_folder}")

# Main function to sort the screenshots
def sort_screenshots():
    if not os.path.exists(source_dir):
        print(f"Source directory {source_dir} does not exist.")
        return

    # Loop through all files in the source directory
    for file_name in os.listdir(source_dir):
        file_path = os.path.join(source_dir, file_name)

        # Check if it's a valid image (screenshot)
        if os.path.isfile(file_path) and is_screenshot(file_path):
            move_file_by_date(file_path, destination_dir)
        else:
            print(f"Skipping {file_path}, not a screenshot.")

# Run the sorting function
sort_screenshots()
How the Script Works:

is_screenshot(file_path): This function checks if the file is an image by using the Pillow library. It attempts to open the file and verify it as a valid image. If the image cannot be opened or verified, it's skipped.

get_creation_date(file_path): Uses the file's creation timestamp to get the date it was created. This will be used to organize the files.

move_file_by_date(file_path, dest_dir): Moves the screenshot file into folders based on the year and month of creation. For example, a screenshot created in January 2022 would go into a folder like /2022/January/.

sort_screenshots(): Loops through all files in the source directory, checks if they are valid images, and moves them to the appropriate folder.

How to Use:

Replace /path/to/your/screenshots with the path to the folder where your screenshots are currently stored.

Replace /path/to/sorted/screenshots with the path to the folder where you'd like to organize your screenshots.

Run the script, and it will sort the screenshots by their creation date into folders categorized by year and month.

Let me know if you need any further adjustments or enhancements for your needs!
