import re

def extract_leading_number(s):
    """"""
    match = re.match(r'(\d+)(?:-|$)', s)
    return "F" + match.group(1) if match else None

