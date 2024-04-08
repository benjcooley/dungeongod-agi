import os
import yaml

def list_files(directory, subdir):
    """ Recursively list all files in the given directory """
    file_paths = []
    walkdir = os.path.join(directory, subdir)
    for root, dirs, files in os.walk(walkdir):
        for file in files:
            if file.startswith("."):
                continue
            # Construct relative file path
            file_path = os.path.relpath(os.path.join(root, file), start=directory)
            file_paths.append(file_path)
    return file_paths

def create_yaml_manifest(file_paths, output_file):
    """ Create a YAML file with the list of file paths """
    with open(output_file, 'w') as file:
        yaml.dump(file_paths, file, default_flow_style=False)

def main():
    manifest_file = 'data-manifest.yaml'

    # List all files in the data directory
    files = list_files(os.getcwd(), "data")

    # Create YAML manifest file
    create_yaml_manifest(files, manifest_file)
    print(f"Manifest created: {manifest_file}")

if __name__ == "__main__":
    main()
