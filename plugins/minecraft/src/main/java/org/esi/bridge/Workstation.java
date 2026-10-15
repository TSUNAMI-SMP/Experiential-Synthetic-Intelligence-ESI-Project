package org.esi.bridge;

import com.google.gson.JsonObject;
import java.util.*;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.player.StackedItemContents;
import net.minecraft.world.inventory.*;
import net.minecraft.world.item.crafting.*;
import net.minecraft.world.item.crafting.display.SlotDisplayContext;

/** Uses vanilla recipe placement/result slots and storage menus, never invents items. */
final class Workstation {
    private BlockPos anchor;
    private String targetId = "";
    void opened(Sense.Target target) {
        if (target != null && target.block() != null) { anchor = target.block(); targetId = target.id(); }
    }
    void close(ServerPlayer player) { player.closeContainer(); anchor = null; targetId = ""; }
    private boolean valid(ServerPlayer player) {
        if (player.containerMenu == player.inventoryMenu) return false;
        if (anchor == null || player.distanceToSqr(anchor.getX()+.5,anchor.getY()+.5,anchor.getZ()+.5)>20.25
                || !player.containerMenu.stillValid(player)) { close(player); return false; }
        return true;
    }
    private boolean storage(AbstractContainerMenu menu) {
        return menu instanceof ChestMenu || menu instanceof HopperMenu || menu instanceof ShulkerBoxMenu;
    }
    Map<String,Object> container(ServerPlayer player) {
        if (!valid(player) || !storage(player.containerMenu)) return null;
        var menu = player.containerMenu;
        List<Map<String,Object>> items = new ArrayList<>();
        for (Slot slot : menu.slots) {
            if (slot.container == player.getInventory() || !slot.hasItem()) continue;
            var stack = slot.getItem();
            items.add(Map.of("slot",slot.index,"name",Sense.itemName(stack),"count",stack.getCount()));
        }
        return Map.of("id",menu.containerId,"kind",player.level().getBlockState(anchor).getBlock().getDescriptionId(),
                      "target_id",targetId,"items",items,"x",anchor.getX()+.5,"y",anchor.getY()+.5,"z",anchor.getZ()+.5);
    }
    private AbstractCraftingMenu crafting(ServerPlayer player) {
        if (player.containerMenu == player.inventoryMenu) return player.inventoryMenu;
        return valid(player) && player.containerMenu instanceof CraftingMenu menu ? menu : null;
    }
    int grid(ServerPlayer player) {
        var menu=crafting(player); return menu==null?0:menu.getGridWidth();
    }
    private static boolean fits(CraftingRecipe recipe, int width, int height) {
        return recipe instanceof ShapedRecipe shaped ? shaped.getWidth()<=width && shaped.getHeight()<=height
            : recipe.placementInfo().ingredients().size()<=width*height;
    }
    List<Map<String,Object>> recipes(ServerPlayer player) {
        var manager = player.level().getServer().getRecipeManager();
        var context = SlotDisplayContext.fromLevel(player.level());
        var contents = new StackedItemContents(); player.getInventory().fillStackedContents(contents);
        var menu = crafting(player);
        if (menu != null) menu.fillCraftSlotsStackedContents(contents);
        List<Map<String,Object>> result = new ArrayList<>();
        for (var key : player.getRecipeBook().pack().known()) {
            var holder = manager.byKey(key).orElse(null);
            if (holder == null || !(holder.value() instanceof CraftingRecipe recipe) || recipe.isSpecial()
                    || recipe.placementInfo().isImpossibleToPlace() || recipe.display().isEmpty()) continue;
            var output = recipe.display().getFirst().result().resolveForFirstStack(context);
            if (output.isEmpty()) continue;
            List<String> ingredients = recipe.placementInfo().ingredients().stream().limit(9)
                .map(i -> i.display().resolveForStacks(context).stream().limit(3).map(Sense::itemName)
                    .reduce((a,b) -> a+" | "+b).orElse("unknown")).toList();
            boolean available = menu != null && fits(recipe,menu.getGridWidth(),menu.getGridHeight()) && contents.canCraft(recipe,null);
            result.add(Map.of("id",key.identifier().toString(),"output",Sense.itemName(output),"count",output.getCount(),
                "ingredients",ingredients,"station",fits(recipe,2,2)?"inventory":"crafting_table",
                "craftable",available,"basis","unlocked_recipe_book"));
        }
        result.sort(Comparator.<Map<String,Object>,Boolean>comparing(r -> !Boolean.TRUE.equals(r.get("craftable")))
            .thenComparing(r -> r.get("id").toString()));
        return result.size()>96 ? new ArrayList<>(result.subList(0,96)) : result;
    }
    void craft(ServerPlayer player, JsonObject request, Sense.View view) {
        String id = Bridge.text(request,"recipe_id",160);
        if (view.recipes().stream().noneMatch(r -> id.equals(r.get("id")) && Boolean.TRUE.equals(r.get("craftable"))))
            throw new IllegalArgumentException("観察した、材料の足りる解放済みレシピを選んでください。");
        var key = ResourceKey.create(Registries.RECIPE,Identifier.parse(id));
        var holder = player.level().getServer().getRecipeManager().byKey(key).orElseThrow();
        var menu = crafting(player);
        if (!player.getRecipeBook().contains(key) || menu == null || !(holder.value() instanceof CraftingRecipe recipe)
                || !fits(recipe,menu.getGridWidth(),menu.getGridHeight()))
            throw new IllegalArgumentException("今の作業場所では、そのレシピを作れません。");
        // false: one recipe, normal survival placement. Vanilla checks materials and returns remainders.
        menu.handlePlacement(false,false,holder,player.level(),player.getInventory());
        if (!menu.getResultSlot().hasItem()) throw new IllegalArgumentException("材料または作業場所が変わり、完成品を確認できません。");
        if (menu.quickMoveStack(player,menu.getResultSlot().index).isEmpty())
            throw new IllegalArgumentException("完成品を所持品へ移せません。空きを確認してください。");
        menu.broadcastChanges();
    }
    void transfer(ServerPlayer player, JsonObject request, Sense.View view, boolean deposit) {
        var now = container(player);
        if (now == null || view.container() == null || !now.get("id").equals(view.container().get("id"))
                || Bridge.number(request,"container_id",0) != ((Number)now.get("id")).intValue())
            throw new IllegalArgumentException("同じ収納を開いたまま、中身を確認してから操作してください。");
        var menu = player.containerMenu;
        int index;
        if (deposit) {
            int slot = (int) Bridge.bounded(request,"slot",0,0,35);
            var old = view.inventory().stream().filter(i -> ((Number)i.get("slot")).intValue()==slot).findFirst().orElseThrow();
            if (!old.get("name").equals(Sense.itemName(player.getInventory().getItem(slot))))
                throw new IllegalArgumentException("所持品が観察から変わりました。");
            index = menu.findSlot(player.getInventory(),slot).orElseThrow();
        } else {
            index = (int) Bridge.bounded(request,"container_slot",0,0,89);
            var old = ((List<Map<String,Object>>)view.container().get("items")).stream()
                .filter(i -> ((Number)i.get("slot")).intValue()==index).findFirst().orElseThrow();
            if (!menu.isValidSlotIndex(index) || menu.getSlot(index).container == player.getInventory()
                    || !old.get("name").equals(Sense.itemName(menu.getSlot(index).getItem())))
                throw new IllegalArgumentException("収納の中身が観察から変わりました。");
        }
        menu.clicked(index,0,ClickType.QUICK_MOVE,player); menu.broadcastChanges();
    }
}
