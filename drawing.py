from PIL import Image, ImageDraw, ImageFont
import math
from io import BytesIO

def get_wrapped_text(text: str, font: ImageFont.ImageFont, line_length: int):
    lines = ['']
    for word in text.split():
        line = f'{lines[-1]} {word}'.strip()
        if font.getlength(line) <= line_length:
            lines[-1] = line
        else:
            lines.append(word)
    return '\n'.join(lines)

def get_font_height(font: ImageFont) -> int:
    ascent, descent = font.getmetrics()
    offset_x, offset_y = font.font.getsize("Sample")[1]
    return int(offset_x + ascent - offset_y + descent)

width=800
name_pt_size=28
text_pt_size=20
margin=12
corner_radius=8
regular_font_path="data/fonts/Merriweather-Regular.ttf"
font = ImageFont.truetype(regular_font_path, text_pt_size)
font_height = get_font_height(font)
bold_font_path="data/fonts/LondrinaSolid-Regular.ttf"
font_bold = ImageFont.truetype(bold_font_path, name_pt_size)
font_bold_height = get_font_height(font_bold)
portrait_size=(84, 84)

def draw_dialog_image(portrait_file: str, character_name: str, dialog_text: str) -> bytes:

    # Load portrait, scale it, and load font
    portrait = Image.open(portrait_file).resize(portrait_size)
    
    # Create a mask for the rounded corners
    mask = Image.new("L", portrait_size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, portrait_size[0], portrait_size[1]], radius=corner_radius, fill=255)
    
    # Calculate space needed for text using the get_wrapped_text function
    wrap_width = width - (portrait.width + 3 * margin)
    wrapped_text = get_wrapped_text(dialog_text, font, wrap_width)
    num_lines = wrapped_text.count('\n') + 1  # +1 to account for the last line
    text_height = font_height * num_lines
        
    # Determine the spacing based on 0.5 times the height of a single line of regular text
    text_spacing = math.ceil(0.5 * font_height)
    
    # Determine the final height of the image
    text_area_height = margin + font_bold_height + text_spacing + text_height + margin
    required_height = max(portrait.height + 2 * margin, text_area_height)
    
    # Create blank black image with computed dimensions
    img = Image.new("RGB", (width, required_height), (40, 40, 40))
    draw = ImageDraw.Draw(img)
    
    # Paste portrait onto image with rounded corners
    portrait_coords = (margin, margin)
    img.paste(portrait, portrait_coords, mask)
    
    # Draw the character name and dialog text
    name_coords = (portrait.width + 2 * margin, margin)
    text_coords = (portrait.width + 2 * margin, margin + font_bold_height + text_spacing)
    
    draw.text(name_coords, character_name, font=font_bold, fill="white")
    draw.text(text_coords, wrapped_text, font=font, fill="white")
    
    # Save the image as a PNG file
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    
    return buffer.getvalue()
