from PIL import Image, ImageDraw, ImageFont
import math
from io import BytesIO
from typing import cast

def get_wrapped_text(text: str, font: ImageFont.ImageFont, line_length: int):
    lines = ['']
    for word in text.split():
        line = f'{lines[-1]} {word}'.strip()
        if font.getlength(line) <= line_length:
            lines[-1] = line
        else:
            lines.append(word)
    return '\n'.join(lines)

def get_font_height(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    offset_x, offset_y = font.font.getsize("Sample")[1]
    return int(offset_x + ascent - offset_y + descent)

class NineSliceImage:
    def __init__(self, image_path, slice_values):
        """
        Initialize the NineSliceImage with the source image and slice values.
        
        Parameters:
        - image_path: The path to the source image for the nine-slice.
        - slice_values: Tuple of four values (top, right, bottom, left) that define the slices.
        """
        
        source_image = Image.open(image_path)
        top, right, bottom, left = slice_values

        # Sections of the source image
        self.top_left = source_image.crop((0, 0, left, top))
        self.top_center = source_image.crop((left, 0, source_image.width - right, top))
        self.top_right = source_image.crop((source_image.width - right, 0, source_image.width, top))

        self.center_left = source_image.crop((0, top, left, source_image.height - bottom))
        self.center = source_image.crop((left, top, source_image.width - right, source_image.height - bottom))
        self.center_right = source_image.crop((source_image.width - right, top, source_image.width, source_image.height - bottom))

        self.bottom_left = source_image.crop((0, source_image.height - bottom, left, source_image.height))
        self.bottom_center = source_image.crop((left, source_image.height - bottom, source_image.width - right, source_image.height))
        self.bottom_right = source_image.crop((source_image.width - right, source_image.height - bottom, source_image.width, source_image.height))
    
    def draw(self, target_image, target_rect):
        """
        Draw the nine slice image on a target image.
        
        Parameters:
        - target_image: The image on which to draw the nine-slice image.
        - target_rect: Tuple of (left, top, width, height) defining the rectangle for the nine-slice image.
        
        Returns:
        - Image with the nine-slice drawn on the target image.
        """

        x, y, target_width, target_height = target_rect

        # Resize center sections to fit target size
        top_center = self.top_center.resize((target_width - self.top_left.width - self.top_right.width, self.top_center.height))
        center_left = self.center_left.resize((self.center_left.width, target_height - self.top_left.height - self.bottom_left.height))
        center = self.center.resize((target_width - self.top_left.width - self.top_right.width, target_height - self.top_left.height - self.bottom_left.height))
        center_right = self.center_right.resize((self.center_right.width, target_height - self.top_right.height - self.bottom_right.height))
        bottom_center = self.bottom_center.resize((target_width - self.bottom_left.width - self.bottom_right.width, self.bottom_center.height))

        # Draw sections on target image
        target_image.paste(self.top_left, (x, y))
        target_image.paste(top_center, (x + self.top_left.width, y))
        target_image.paste(self.top_right, (x + target_width - self.top_right.width, y))
        target_image.paste(center_left, (x, y + self.top_left.height))
        target_image.paste(center, (x + self.top_left.width, y + self.top_left.height))
        target_image.paste(center_right, (x + target_width - self.top_right.width, y + self.top_right.height))
        target_image.paste(self.bottom_left, (x, y + target_height - self.bottom_left.height))
        target_image.paste(bottom_center, (x + self.bottom_left.width, y + target_height - self.bottom_left.height))
        target_image.paste(self.bottom_right, (x + target_width - self.bottom_right.width, y + target_height - self.bottom_right.height))

dlg_width=550
dlg_name_pt_size=18
dlg_text_pt_size=14
dlg_margin=12
dlg_corner_radius=8
dlg_regular_font_path="data/fonts/Merriweather-Regular.ttf"
dlg_font: ImageFont.FreeTypeFont|None = None
dlg_font_height: int = 0
dlg_bold_font_path="data/fonts/LondrinaSolid-Regular.ttf"
dlg_font_bold: ImageFont.FreeTypeFont|None = None
dlg_font_bold_height: int = 0
dlg_portrait_size=(64, 64)

def draw_dialog_image(portrait_file: str, character_name: str, dialog_text: str) -> bytes:

    global dlg_font
    global dlg_font_height
    global dlg_font_bold
    global dlg_font_bold_height

    if dlg_font is None:
        dlg_font = ImageFont.truetype(dlg_regular_font_path, dlg_text_pt_size)
        dlg_font_height = get_font_height(dlg_font)
        dlg_font_bold = ImageFont.truetype(dlg_bold_font_path, dlg_name_pt_size)
        dlg_font_bold_height = get_font_height(dlg_font_bold)

    assert dlg_font is not None 
    assert dlg_font_bold is not None

    # Load portrait, scale it, and load font
    portrait = Image.open(portrait_file).resize(dlg_portrait_size)
    
    # Create a mask for the rounded corners
    mask = Image.new("L", dlg_portrait_size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, dlg_portrait_size[0], dlg_portrait_size[1]), radius=dlg_corner_radius, fill=255)
    
    # Calculate space needed for text using the get_wrapped_text function
    wrap_width = dlg_width - (portrait.width + 3 * dlg_margin)
    wrapped_text = get_wrapped_text(dialog_text, cast(ImageFont.ImageFont, dlg_font), wrap_width)
    num_lines = wrapped_text.count('\n') + 1  # +1 to account for the last line
    text_height = (dlg_font_height + 1) * num_lines
        
    # Determine the spacing based on 0.5 times the height of a single line of regular text
    text_spacing = math.ceil(0.5 * dlg_font_height)
    
    # Determine the final height of the image
    text_area_height = dlg_margin + dlg_font_bold_height + text_spacing + text_height + dlg_margin
    required_height = max(portrait.height + 2 * dlg_margin, text_area_height)
    
    # Create blank black image with computed dimensions
    img = Image.new("RGB", (dlg_width, required_height), (35, 35, 35))
    draw = ImageDraw.Draw(img)
    
    # Paste portrait onto image with rounded corners
    portrait_coords = (dlg_margin, dlg_margin)
    img.paste(portrait, portrait_coords, mask)
    
    # Draw the character name and dialog text
    name_coords = (portrait.width + 2 * dlg_margin, dlg_margin)
    text_coords = (portrait.width + 2 * dlg_margin, dlg_margin + dlg_font_bold_height + text_spacing)
    
    draw.text(name_coords, character_name, font=dlg_font_bold, fill="white")
    draw.text(text_coords, wrapped_text, font=dlg_font, fill="white")
    
    # Save the image as a PNG file
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    
    return buffer.getvalue()

# Load portrait, scale it, and load font
brown_frame = NineSliceImage("data/licensed/ui/frame/fr_02_half.png", (20, 44, 20, 44))
paper_bg = Image.open("data/licensed/ui/paper/ppr_02.png")

def draw_frame_bg(target_image: Image.Image, rect: tuple) -> None:
    left, top, width, height = rect
    target_image.paste(paper_bg.resize((width, height)), (left, top))
    brown_frame.draw(target_image, rect)

def draw_inventory_image(portrait_file: str, character_name: str, dialog_text: str) -> bytes:

    # Load portrait, scale it, and load font
    portrait = Image.open(portrait_file).resize(dlg_portrait_size)
    
    # Create a mask for the rounded corners
    mask = Image.new("L", dlg_portrait_size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, dlg_portrait_size[0], dlg_portrait_size[1]), radius=dlg_corner_radius, fill=255)
    
    # Calculate space needed for text using the get_wrapped_text function
    width = 800
    wrap_width = width - (portrait.width + 3 * dlg_margin)
    wrapped_text = get_wrapped_text(dialog_text, dlg_font, wrap_width)
    num_lines = wrapped_text.count('\n') + 1  # +1 to account for the last line
    text_height = dlg_font_height * num_lines
        
    # Determine the spacing based on 0.5 times the height of a single line of regular text
    text_spacing = math.ceil(0.5 * dlg_font_height)
    
    # Determine the final height of the image
    text_area_height = dlg_margin + dlg_font_bold_height + text_spacing + text_height + dlg_margin
    required_height = max(portrait.height + 2 * dlg_margin, text_area_height)
    
    # Create blank black image with computed dimensions
    img = Image.new("RGB", (width, required_height), (35, 35, 35))
    draw = ImageDraw.Draw(img)
    
    # Paste portrait onto image with rounded corners
    portrait_coords = (dlg_margin, dlg_margin)
    img.paste(portrait, portrait_coords, mask)
    
    # Draw the character name and dialog text
    name_coords = (portrait.width + 2 * dlg_margin, dlg_margin)
    text_coords = (portrait.width + 2 * dlg_margin, dlg_margin + dlg_font_bold_height + text_spacing)
    
    draw.text(name_coords, character_name, font=dlg_font_bold, fill="white")
    draw.text(text_coords, wrapped_text, font=dlg_font, fill="white")
    
    # Save the image as a PNG file
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    
    return buffer.getvalue()
