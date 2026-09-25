"""Conservative recipe/budget screen. NOT a complete LoL shop legality engine.

Separates mechanically feasible candidates from statistical estimation. Exact
budget and inventory must be supplied; stale timeline gold is not an exact budget.
Unsupported special-item rules are rejected instead of guessed.
"""
from collections import Counter

def recipe_cost(item_id, inventory, items):
    item_id = str(item_id)
    item = items[item_id]
    owned = Counter(str(x) for x in inventory)
    consumed = Counter()
    def use(component,seen):
        component = str(component)
        if component in seen:
            raise ValueError("Cyclic item recipe")
        if owned[component]:
            owned[component] -= 1
            consumed[component] += 1
            return items[component]["gold"]["total"]
        return sum(use(child,seen|{component}) for child in items[component].get("from",[]))
    discount = sum(use(child,{item_id}) for child in item.get("from",[]))
    return max(0,item["gold"]["total"]-discount),consumed

def screen(item_ids, inventory, budget, items):
    """Return budget/slot candidates and exclusions; game-specific rules still need validation."""
    if budget < 0 or any(str(i) not in items for i in inventory):
        raise ValueError("Supply nonnegative exact budget and known inventory IDs")
    # Only non-trinket inventory occupies these six slots.
    slots = sum('Trinket' not in items[str(i)].get('tags',[]) for i in inventory)
    results = []
    for item_id in item_ids:
        key = str(item_id)
        item = items.get(key)
        reason = None
        if not item:
            reason = 'unknown item'
        elif not item.get('maps',{}).get('11') or not item.get('gold',{}).get('purchasable') or item.get('inStore') is False:
            reason = 'not directly purchasable on Summoner’s Rift'
        elif item.get('requiredChampion') or item.get('requiredAlly') or item.get('specialRecipe') or item.get('hideFromAll') or 'Trinket' in item.get('tags',[]) or 'Consumable' in item.get('tags',[]):
            reason = 'special rules not implemented'
        elif key in map(str,inventory):
            reason = 'duplicate purchase not evaluated'
        if reason:
            results.append({'id':key,'supported':False,'reason':reason})
            continue
        cost,consumed = recipe_cost(key,inventory,items)
        after = slots-sum(consumed.values())+1
        results.append({'id':key,'supported':True,'recipeCost':cost,'consumes':dict(consumed),
                        'budgetAndSlotsPass':cost<=budget and after<=6,
                        'gameLegalityVerified':False,
                        'caveat':'Unique groups, transformations and champion rules require further validation; not a recommendation'})
    return results
