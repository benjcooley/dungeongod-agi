from datetime import datetime, timedelta
import re
import os

def find_case_insensitive(dic: dict, key: str) -> tuple[str, any]:
    # Unique name will always be the right case (i.e "Dagger#1001")
    value = dic.get(key)
    if value is not None:
        return (key, value)
    # Search the dictionary linearly for non unique name. Sometimes this 
    # may not match case as the AI sometimes doesn't get casing right.
    lower_key = key.casefold()
    for k, v in dic.items():
        # We use the "name" prop if it has one, otherwise use k
        if isinstance(v, dict):
            name = v.get("name") or k
        else:
            name = k
        if name.casefold() == lower_key:
            # Always return the key as the name
            return (k, v)
    return (None, None)

def any_to_int(val: any) -> tuple[int, bool]:
    if isinstance(val, int):
        return (val, False)
    if not isinstance(val, str):
        return (0, True)
    s = val.strip()
    p = len(s) - 1
    while p >= 0:
        if s[p].isdigit():
            return (int(s[:p + 1]), False)
    return (0, True)

def parse_date_time(time_str: str) -> datetime:
    return datetime.strptime(time_str, "%b %d %Y %H:%M")   

def time_difference_mins(time1_str, time2_str) -> int:
    delta = parse_date_time(time1_str) - parse_date_time(time2_str)
    return int(delta.total_seconds() / 60)

def escape_path_key(key: str) -> str:
    key = key.replace("\\", r"\\")
    key = key.replace(".", r"\.")
    return key

def is_valid_filename(filename):
    # Check length
    if len(filename) > 30:
        return False
    
    # Forbidden characters for Windows, Linux, macOS
    forbidden_chars = r'[<>:"/\\|?*]'
    
    # Expand the allowed punctuation to include spaces, &, +, -
    # This pattern allows Unicode word characters (\w, which includes letters, digits, and underscores),
    # spaces, hyphens, periods, &, and +.
    # \u00A0-\uFFFF range includes a vast majority of common Unicode characters.
    allowed_punctuation = r'^[\w \-&\.\+\u00A0-\uFFFF]+$'
    
    # Check if filename contains forbidden characters
    if re.search(forbidden_chars, filename):
        return False
    
    # Check if filename contains only allowed punctuation
    if not re.match(allowed_punctuation, filename):
        return False
    
    return True

def check_for_image(base_path: str, name: str, type_name: str = None) -> str:
    name = os.path.splitext(name)[0]
    exts = [ ".jpg", ".png", ".gif" ]
    if type_name is not None:
        paths = [ f"/{type_name}", "" ]
    else:
        paths = [ "" ]
    for ext in exts:
        for path in paths:
            image_path = base_path + path + f"/{name}{ext}"
            if os.path.exists(image_path):
                return image_path
    return None

def extract_arguments(text: str, num_args: int) -> list[str]:
    pattern = r'do_action\(([^)]*)\)'  # Matches the pattern do_action(args)
    match = re.search(pattern, text)
    
    if not match:
        return []
    
    args_str = match.group(1)  # Get the arguments part of the pattern

    # Now extract the quoted strings, integers, and floats from the arguments
    arg_pattern = r'("([^"]*)"|(\d+\.\d+|\d+))'
    args_matches = re.findall(arg_pattern, args_str)

    # Convert captured arguments into proper types (strings, ints, or floats)
    args = []
    for match in args_matches:
        if match[1]:  # Quoted string
            args.append(match[1])
        elif match[2]:  # Number (int or float)
            num_str = match[2]
            if '.' in num_str:
                args.append(float(num_str))
            else:
                args.append(int(num_str))

    # Ensure there are always 6 arguments
    while len(args) < num_args:
        args.append(None)
    return args[:num_args]
