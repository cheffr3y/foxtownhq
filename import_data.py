import json
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

def get_db():
    return psycopg2.connect(os.getenv('DATABASE_URL'))

def import_data():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Load both JSON files
    print("Loading JSON files...")
    with open('banquet_backup_2026-02-04.json', 'r') as f:
        banquet_data = json.load(f)
    
    with open('foxtown_commissary_backup.json', 'r') as f:
        commissary_data = json.load(f)
    
    # Merge and deduplicate ingredients by name
    print("\nProcessing ingredients...")
    ingredients_map = {}  # name -> ingredient data
    
    for ing in banquet_data['ingredients'] + commissary_data['ingredients']:
        name = ing['name'].strip()
        if name not in ingredients_map:
            ingredients_map[name] = ing
    
    print(f"Found {len(ingredients_map)} unique ingredients")
    
    # Import ingredients
    imported_ingredients = 0
    for name, ing in ingredients_map.items():
        ing_id = f"ing_{ing['id']}"
        
        try:
            cur.execute("""
                INSERT INTO ingredients (id, name, category, unit, vendor, vendor_code, g_code, cost_per_unit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (
                ing_id,
                ing['name'],
                ing.get('category', 'Uncategorized'),
                ing.get('uom', ''),
                ing.get('vendor', ''),
                None,  # vendor_code
                None,  # g_code
                None   # cost_per_unit
            ))
            imported_ingredients += 1
        except Exception as e:
            print(f"Error importing ingredient {name}: {e}")
    
    conn.commit()
    print(f"Imported {imported_ingredients} ingredients")
    
    # Merge and deduplicate recipes by name
    print("\nProcessing recipes...")
    recipes_map = {}  # name -> recipe data
    
    for recipe in banquet_data['recipes'] + commissary_data['recipes']:
        name = recipe['name'].strip()
        if name not in recipes_map:
            recipes_map[name] = recipe
    
    print(f"Found {len(recipes_map)} unique recipes")
    
    # Import recipes
    imported_recipes = 0
    for name, recipe in recipes_map.items():
        recipe_id = f"rec_{recipe['id']}"
        
        # Join steps into instructions
        instructions = '\n\n'.join(recipe.get('steps', [])) if 'steps' in recipe else ''
        
        try:
            cur.execute("""
                INSERT INTO recipes (id, name, category, yield_qty, yield_unit, instructions)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (
                recipe_id,
                recipe['name'],
                'Uncategorized',  # These files don't have recipe categories
                recipe.get('yield_qty', 1),
                recipe.get('yield_unit', ''),
                instructions
            ))
            imported_recipes += 1
        except Exception as e:
            print(f"Error importing recipe {name}: {e}")
    
    conn.commit()
    print(f"Imported {imported_recipes} recipes")
    
    # Import recipe ingredients
    print("\nProcessing recipe ingredients...")
    imported_ingredients_count = 0
    
    for name, recipe in recipes_map.items():
        recipe_id = f"rec_{recipe['id']}"
        
        for ing in recipe.get('ingredients', []):
            # Skip if no name
            if 'name' not in ing:
                print(f"WARNING: Ingredient without name in recipe '{name}', skipping")
                continue
                
            ing_name = ing['name'].strip()
            
            # Find matching ingredient ID
            cur.execute("SELECT id FROM ingredients WHERE name = %s", (ing_name,))
            result = cur.fetchone()
            
            if result:
                ing_id = result['id']
                
                try:
                    cur.execute("""
                        INSERT INTO recipe_ingredients (recipe_id, type, item_id, quantity, unit)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        recipe_id,
                        'ingredient',
                        ing_id,
                        ing.get('quantity', 0),
                        ing.get('unit', '')
                    ))
                    imported_ingredients_count += 1
                except Exception as e:
                    print(f"Error linking ingredient {ing_name} to recipe {name}: {e}")
            else:
                print(f"WARNING: Ingredient '{ing_name}' not found for recipe '{name}'")
    
    conn.commit()
    print(f"Imported {imported_ingredients_count} recipe-ingredient relationships")
    
    cur.close()
    conn.close()
    
    print("\n✓ Import complete!")
    print("\nNext steps:")
    print("1. Edit ingredients to add vendor, vendor_code, g_code, and costs")
    print("2. Categorize recipes")
    print("3. Identify and link sub-recipes")

if __name__ == '__main__':
    import_data()