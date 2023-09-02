
import os
import PIL
from PIL import Image, ImageDraw, ImageFont, ImageFilter

image_cache: dict[str, any] = {}
font_cache: dict[str, PIL.FreeTypeFont] = {}

def rgb_to_hex(r: int, g: int, b: int) -> str:
    return '#{0:02x}{1:02x}{2:02x}'.format(r, g, b)

def fit_text_to_box_width_priority(text, box_width: float, font_path, max_font_size: int, min_font_size: int) -> PIL.FreeTypeFont:
    for font_size in range(max_font_size, min_font_size - 1, -1):
        font = ImageFont.truetype(font_path, font_size)
        if font.getsize(text)[0] <= box_width:
            return font
    return ImageFont.truetype(font_path, min_font_size)

class Drawing:

    def __init(self):
        self.i

def justify_text(draw, text: str, font: any, x: float, y: float, width: float, height: float, align_horizontal: str, align_vertical: str):
    text_width, text_height = draw.textsize(text, font=font)
    if align_horizontal == "CENTER":
        x += (width - text_width) / 2
    elif align_horizontal == "RIGHT":
        x += width - text_width
    if align_vertical == "CENTER":
        y += (height - text_height) / 2
    return x, y

def calculate_effect_buffer_size(x, y, width, height, effect_dict):
    border = 0
    if "DropShadow" in effect_dict:
        shadow_offset = effect_dict["DropShadow"]["Offset"]
        blur_radius = effect_dict["DropShadow"].get("Blur", 0)
        border = max(abs(shadow_offset[0]), abs(shadow_offset[1])) + blur_radius
    left = x - border
    top = y - border
    buffer_width = width + 2 * border
    buffer_height = height + 2 * border
    return left, top, buffer_width, buffer_height

def apply_text_effects_optimized(draw, text, x, y, font, effect_dict):
    left, top, buffer_width, buffer_height = calculate_effect_buffer_size(x, y, font.getsize(text)[0], font.getsize(text)[1], effect_dict)
    left, top, buffer_width, buffer_height = int(left), int(top), int(buffer_width), int(buffer_height)
    adjusted_x = int(x - left)
    adjusted_y = int(y - top)

    shadow_img = Image.new("RGBA", (buffer_width, buffer_height), (0, 0, 0, 0)) if "DropShadow" in effect_dict else None
    text_img = Image.new("RGBA", (buffer_width, buffer_height), (0, 0, 0, 0))

    if shadow_img:
        shadow_draw = ImageDraw.Draw(shadow_img)
        shadow_offset = effect_dict["DropShadow"]["Offset"]
        blur_radius = effect_dict["DropShadow"].get("Blur", 0)
        shadow_draw.text((adjusted_x + shadow_offset[0], adjusted_y + shadow_offset[1]), text, font=font, fill=(0, 0, 0, 255))
        if blur_radius > 0:
            alpha_channel = shadow_img.split()[3]
            blurred_alpha = alpha_channel.filter(ImageFilter.GaussianBlur(blur_radius))
            shadow_img.putalpha(blurred_alpha)

    text_draw = ImageDraw.Draw(text_img)
    if "Outline" in effect_dict:
        outline_size = effect_dict["Outline"]["Size"]
        outline_color = effect_dict["Outline"]["Color"]
        for i in range(-outline_size, outline_size + 1):
            for j in range(-outline_size, outline_size + 1):
                text_draw.text((adjusted_x + i, adjusted_y + j), text, font=font, fill=outline_color)
    text_draw.text((adjusted_x, adjusted_y), text, font=font)
    return shadow_img, text_img, left, top

def render_layout_with_optimized_effects(data_dict):
    main_canvas = Image.new("RGBA", (total_width, total_height), (255, 255, 255, 0))
    main_draw = ImageDraw.Draw(main_canvas)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    max_font_size = 40
    min_font_size = 10

    for layer in group4_data['children']:
        if layer['type'] == 'RECTANGLE' and 'fills' in layer and layer['fills'][0]['type'] == 'IMAGE':
            img_path = os.path.join(extract_folder, layer['fills'][0]['src'])
            img = Image.open(img_path)
            img = img.resize((int(layer['width']), int(layer['height'])))
            mask = img if img.mode == 'RGBA' else None
            main_canvas.paste(img, (int(layer['x'] - x_offset), int(layer['y'] - y_offset)), mask)

        elif layer['type'] == 'TEXT':
            text = data_dict.get('Texts', {}).get(layer['name'], layer['name'])
            font = fit_text_to_box_width_priority(text, layer['width'], font_path, max_font_size, min_font_size)
            r, g, b = layer['fills'][0]['color']['r'], layer['fills'][0]['color']['g'], layer['fills'][0]['color']['b']
            text_color = rgb_to_hex(int(r * 255), int(g * 255), int(b * 255))
            align_horizontal = layer['textAlignHorizontal']
            align_vertical = "CENTER"
            x, y = justify_text(main_draw, text, font, layer['x'] - x_offset, layer['y'] - y_offset, layer['width'], layer['height'], text_color, align_horizontal, align_vertical)
            
            effect_name = data_dict["TextEffects"].get(layer['name'])
            if effect_name:
                effect_dict = data_dict["Effects"].get(effect_name)
                if effect_dict:
                    shadow_img, text_img, left, top = apply_text_effects_optimized(main_draw, text, x, y, font, effect_dict)
                    if shadow_img:
                        main_canvas.paste(shadow_img, (left, top), shadow_img)
                    if text_img:
                        main_canvas.paste(text_img, (left, top), text_img)
            else:
                main_draw.text((x, y), text, font=font, fill=text_color)
    return main_canvas
