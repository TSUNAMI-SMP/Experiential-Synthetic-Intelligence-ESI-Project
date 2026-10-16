package org.esi.bridge;

import java.util.*;
import net.minecraft.core.BlockPos;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.BlockItem;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.*;

final class Sense {
    record Target(String id, String kind, String name, Vec3 position, BlockPos block, int entity) {}
    record View(String id, String dimension, long created, Map<String,Object> actor, List<Map<String,Object>> inventory,
                List<Map<String,Object>> targets, Map<String,Target> references,
                List<Map<String,Object>> recipes, Map<String,Object> container) {}
    static String blockName(BlockState state) { return BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString(); }
    static String itemName(ItemStack stack) { return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(); }
    static boolean visible(ServerPlayer player, BlockPos point) {
        var hit = player.level().clip(new ClipContext(player.getEyePosition(), Vec3.atCenterOf(point),
            ClipContext.Block.COLLIDER, ClipContext.Fluid.NONE, player));
        return hit.getType() == HitResult.Type.MISS || hit.getBlockPos().equals(point);
    }
    static boolean clear(ServerPlayer player, Vec3 point) {
        var hit = player.level().clip(new ClipContext(player.getEyePosition(), point,
            ClipContext.Block.COLLIDER, ClipContext.Fluid.NONE, player));
        return hit.getType() == HitResult.Type.MISS;
    }
    static List<Map<String,Object>> inventory(ServerPlayer bot) {
        List<Map<String,Object>> inventory = new ArrayList<>();
        for (int slot=0; slot<Math.min(41, bot.getInventory().getContainerSize()); slot++) {
            ItemStack stack = bot.getInventory().getItem(slot);
            if (stack.isEmpty()) continue;
            Map<String,Object> item = new LinkedHashMap<>();
            item.put("slot", slot); item.put("name", itemName(stack)); item.put("count", stack.getCount());
            item.put("edible", stack.has(DataComponents.FOOD)); item.put("selected", slot==bot.getInventory().getSelectedSlot());
            item.put("placeable", stack.getItem() instanceof BlockItem);
            item.put("durability", stack.isDamageableItem() ? stack.getMaxDamage()-stack.getDamageValue() : null);
            inventory.add(item);
        }
        return inventory;
    }
    static Map<String,Object> frame(ServerPlayer bot, Target target, int selectedSlot) {
        Map<String,Object> frame = new LinkedHashMap<>();
        frame.put("actor_id",bot.getUUID().toString()); frame.put("dimension",bot.level().dimension().identifier().toString());
        frame.put("tick",bot.level().getServer().getTickCount()); frame.put("inventory",inventory(bot));
        frame.put("held",bot.getMainHandItem().isEmpty()?"empty hand":itemName(bot.getMainHandItem()));
        frame.put("selected_item",itemName(bot.getInventory().getItem(Math.max(0,Math.min(35,selectedSlot)))));
        frame.put("food",bot.getFoodData().getFoodLevel()); frame.put("saturation",bot.getFoodData().getSaturationLevel());
        frame.put("health",Math.max(0,bot.getHealth())); frame.put("x",bot.getX()); frame.put("y",bot.getY()); frame.put("z",bot.getZ());
        frame.put("yaw",bot.getYRot()); frame.put("pitch",bot.getXRot());
        frame.put("container_id",bot.containerMenu==bot.inventoryMenu?0:bot.containerMenu.containerId);
        if (target != null) {
            frame.put("target_distance",bot.position().distanceTo(target.position()));
            if (target.block()!=null) frame.put("target_block",blockName(bot.level().getBlockState(target.block())));
        }
        return frame;
    }
    static View observe(ServerPlayer bot, String id, Workstation workstation) {
        Map<String,Object> actor = new LinkedHashMap<>();
        actor.put("id", bot.getUUID().toString()); actor.put("name", bot.getGameProfile().name());
        actor.put("dimension", bot.level().dimension().identifier().toString());
        actor.put("x", bot.getX()); actor.put("y", bot.getY()); actor.put("z", bot.getZ());
        actor.put("yaw", bot.getYRot()); actor.put("pitch", bot.getXRot());
        actor.put("health", Math.max(0, bot.getHealth())); actor.put("max_health", bot.getMaxHealth());
        actor.put("food", bot.getFoodData().getFoodLevel()); actor.put("saturation", bot.getFoodData().getSaturationLevel());
        actor.put("air", Math.max(-20, Math.min(300, bot.getAirSupply())));
        actor.put("dead", bot.isDeadOrDying() || bot.isRemoved()); actor.put("on_ground", bot.onGround());
        actor.put("eye_height",bot.getEyeHeight());
        List<Map<String,Object>> inventory = inventory(bot);
        List<Target> candidates = new ArrayList<>();
        if (!bot.isRemoved() && !bot.isDeadOrDying()) {
            BlockPos feet = bot.blockPosition();
            for (BlockPos mutable : BlockPos.betweenClosed(feet.offset(-8,-3,-8), feet.offset(8,5,8))) {
                BlockPos pos = mutable.immutable();
                BlockState state = bot.level().getBlockState(pos);
                if (state.isAir() || Vec3.atCenterOf(pos).distanceTo(bot.getEyePosition())>8) continue;
                Vec3 direction = Vec3.atCenterOf(pos).subtract(bot.getEyePosition()).normalize();
                if (direction.dot(bot.getLookAngle()) < -0.05 || !visible(bot,pos)) continue;
                candidates.add(new Target("b:"+pos.getX()+","+pos.getY()+","+pos.getZ(),"block",blockName(state),Vec3.atCenterOf(pos),pos,-1));
            }
            // Keep the floor from crowding all visible blocks of the same material out of the view.
            candidates.sort(Comparator.comparingDouble(t -> t.position().distanceTo(bot.position())
                + (t.block().getY() < bot.blockPosition().getY() ? 3 : 0)));
            Map<String,Integer> counts = new HashMap<>();
            candidates.removeIf(t -> counts.merge(t.name(),1,Integer::sum)>8);
            if (candidates.size()>40) candidates = new ArrayList<>(candidates.subList(0,40));
            for (Entity entity : bot.level().getEntities(bot, bot.getBoundingBox().inflate(8), e -> e.isAlive())) {
                if (!bot.hasLineOfSight(entity)) continue;
                String name = entity instanceof ItemEntity item ? "item:"+itemName(item.getItem())
                    : BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
                candidates.add(new Target("e:"+entity.getId(),"entity",name,entity.position(),null,entity.getId()));
                if (candidates.size()>=52) break;
            }
            // Cover every direction and include a one-block exit from shallow holes.
            // The previous lexicographic 12-point cutoff only exposed one side of the player.
            for(int[] offset:new int[][]{{0,4},{4,4},{4,0},{4,-4},{0,-4},{-4,-4},{-4,0},{-4,4},
                    {0,2},{2,2},{2,0},{2,-2},{0,-2},{-2,-2},{-2,0},{-2,2},
                    {0,1},{1,0},{0,-1},{-1,0}}) {
                int dx=offset[0],dz=offset[1];
                for(int dy:new int[]{0,1,-1}) {
                    BlockPos pos=feet.offset(dx,dy,dz);
                    if (!Walking.standable(bot,pos) || !clear(bot,new Vec3(pos.getX()+.5,pos.getY()+1,pos.getZ()+.5))) continue;
                    candidates.add(new Target("w:"+pos.getX()+","+pos.getY()+","+pos.getZ(),"waypoint","見える足場",
                        new Vec3(pos.getX()+.5,pos.getY(),pos.getZ()+.5),pos,-1));
                    break;
                }
            }
        }
        Map<String,Target> references=new LinkedHashMap<>(); List<Map<String,Object>> targets=new ArrayList<>();
        for(Target target:candidates) {
            double distance=bot.getEyePosition().distanceTo(target.position());
            Map<String,Object> value=new LinkedHashMap<>();
            value.put("id",target.id()); value.put("kind",target.kind()); value.put("name",target.name());
            value.put("x",target.position().x); value.put("y",target.position().y); value.put("z",target.position().z);
            value.put("distance",distance);
            value.put("reachable",target.kind().equals("block") ? distance<=4.5 : target.kind().equals("entity") && distance<=3.5
                && !target.name().equals("minecraft:player") && !target.name().startsWith("item:"));
            value.put("harvestable",target.kind().equals("block") && bot.hasCorrectToolForDrops(bot.level().getBlockState(target.block())));
            references.put(target.id(),target); targets.add(value);
        }
        return new View(id,bot.level().dimension().identifier().toString(),System.nanoTime(),actor,inventory,targets,references,
                        workstation.recipes(bot),workstation.container(bot));
    }
}
