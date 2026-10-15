package org.esi.bridge;

import com.google.gson.JsonObject;
import java.util.*;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.Registries;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.inventory.CraftingMenu;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;

public class WorkstationGameTest {
    private static void check(boolean ok,String message) { if(!ok)throw new IllegalStateException(message); }
    private static JsonObject action(Bridge bridge,String kind) {
        JsonObject result=new JsonObject();result.addProperty("kind",kind);
        result.addProperty("request_id",UUID.randomUUID().toString().replace("-",""));
        result.addProperty("observation_id",bridge.state().get("id").toString());
        result.addProperty("ticks",20);return result;
    }
    private static int count(Bridge bridge,net.minecraft.world.item.Item item) {
        int count=0;for(int i=0;i<36;i++)if(bridge.bot.getInventory().getItem(i).is(item))count+=bridge.bot.getInventory().getItem(i).getCount();return count;
    }
    private static String target(BlockPos point) {return "b:"+point.getX()+","+point.getY()+","+point.getZ();}
    @GameTest(maxTicks=240,skyAccess=true)
    public void knownRecipesConsumeMaterialsAndStorageRequiresAnOpenMenu(GameTestHelper helper) throws Exception {
        for(int x=0;x<8;x++)for(int z=0;z<8;z++)helper.setBlock(x,0,z,Blocks.STONE);
        helper.setBlock(4,1,4,Blocks.CRAFTING_TABLE);helper.setBlock(2,1,4,Blocks.CHEST);
        var level=helper.getLevel();var server=level.getServer();var owner=helper.makeMockServerPlayerInLevel();
        BlockPos origin=helper.absolutePos(new BlockPos(3,1,2));
        owner.teleportTo(level,origin.getX()+.5,origin.getY(),origin.getZ()+.5,Set.of(),0,0,true);
        Bridge bridge=new Bridge(server);bridge.authorize(owner);
        JsonObject connect=new JsonObject();connect.addProperty("protocol",1);connect.addProperty("plugin_id","minecraft.fabric");
        connect.addProperty("character_id",UUID.randomUUID().toString());
        String lease=bridge.connect(connect).get("lease").toString();
        check(((List<?>)bridge.state().get("recipes")).isEmpty(),"A new character must not know the world's whole recipe list");
        var invalidSlot=action(bridge,"select_slot");invalidSlot.addProperty("slot",40);
        try {bridge.act(invalidSlot);throw new IllegalStateException("Out-of-range inventory selection was accepted");}catch(IllegalArgumentException expected){}
        bridge.tick();check(((Map<?,?>)bridge.state().get("action")).isEmpty(),"Rejected input must not leave a half-created operation");
        bridge.bot.getInventory().setItem(0,new ItemStack(Items.OAK_LOG,2));
        bridge.bot.getInventory().setItem(1,new ItemStack(Items.STICK,2));
        var manager=server.getRecipeManager();
        for(String recipe:List.of("oak_planks","wooden_pickaxe"))bridge.bot.awardRecipes(List.of(manager.byKey(
            ResourceKey.create(Registries.RECIPE,Identifier.parse("minecraft:"+recipe))).orElseThrow()));
        var illegal=action(bridge,"craft");illegal.addProperty("recipe_id","minecraft:diamond_pickaxe");
        try {bridge.act(illegal);throw new IllegalStateException("Unknown recipe was accepted");}catch(IllegalArgumentException expected){}
        for(int i=0;i<36;i++)bridge.bot.getInventory().setItem(i,new ItemStack(Items.STONE,64));
        bridge.bot.getInventory().setItem(0,new ItemStack(Items.OAK_LOG,64));
        var full=action(bridge,"craft");full.addProperty("recipe_id","minecraft:oak_planks");
        try {bridge.act(full);throw new IllegalStateException("Crafted output into a full inventory");}catch(IllegalArgumentException expected){}
        check(count(bridge,Items.OAK_LOG)==64 && count(bridge,Items.OAK_PLANKS)==0,"Rejected crafting must return its ingredients without duplication or loss");
        bridge.bot.getInventory().clearContent();
        bridge.bot.getInventory().setItem(0,new ItemStack(Items.OAK_LOG,2));
        bridge.bot.getInventory().setItem(1,new ItemStack(Items.STICK,2));
        var planks=action(bridge,"craft");planks.addProperty("recipe_id","minecraft:oak_planks");bridge.act(planks);
        check(count(bridge,Items.OAK_LOG)==1 && count(bridge,Items.OAK_PLANKS)==4,"Crafting must consume exactly one log and produce four planks");
        Map<?,?> evidence=(Map<?,?>)((Map<?,?>)bridge.state().get("action")).get("evidence");
        check(evidence.containsKey("before") && evidence.containsKey("after"),"Missing before/after evidence");
        var tooSmall=action(bridge,"craft");tooSmall.addProperty("recipe_id","minecraft:wooden_pickaxe");
        try {bridge.act(tooSmall);throw new IllegalStateException("3x3 recipe was crafted without a table");}catch(IllegalArgumentException expected){}
        check(bridge.state().get("container")==null,"Closed chest contents must not be exposed");
        var use=action(bridge,"use");use.addProperty("target_id",target(helper.absolutePos(new BlockPos(4,1,4))));bridge.act(use);
        int[] phase={0},ticks={0};
        helper.onEachTick(() -> {
            if(phase[0]>=4)return;
            try {
                ticks[0]++;bridge.checkLease(lease);bridge.tick();
                if(phase[0]==0 && ticks[0]>=8) {
                    check(bridge.bot.containerMenu instanceof CraftingMenu,"Vanilla use must open the crafting table");
                    var craft=action(bridge,"craft");craft.addProperty("recipe_id","minecraft:wooden_pickaxe");bridge.act(craft);
                    check(count(bridge,Items.WOODEN_PICKAXE)==1 && count(bridge,Items.OAK_PLANKS)==1 && count(bridge,Items.STICK)==0,
                        "Table crafting must consume materials and give one tool");
                    // Use an empty hand so vanilla interaction opens rather than placing a block.
                    bridge.bot.getInventory().setSelectedSlot(8);
                    var open=action(bridge,"use");open.addProperty("target_id",target(helper.absolutePos(new BlockPos(2,1,4))));bridge.act(open);
                    phase[0]=1;ticks[0]=0;
                } else if(phase[0]==1 && ticks[0]>=8) {
                    var container=(Map<?,?>)bridge.state().get("container");check(container!=null,"Chest must actually open");
                    int slot=-1;for(int i=0;i<36;i++)if(bridge.bot.getInventory().getItem(i).is(Items.OAK_PLANKS))slot=i;
                    var put=action(bridge,"deposit");put.addProperty("slot",slot);put.addProperty("container_id",((Number)container.get("id")).intValue());bridge.act(put);
                    check(count(bridge,Items.OAK_PLANKS)==0,"Deposit must remove item from player inventory");
                    var stored=(Map<?,?>)bridge.state().get("container");var item=(Map<?,?>)((List<?>)stored.get("items")).getFirst();
                    check(item.get("name").equals("minecraft:oak_planks"),"Real chest contents must contain the deposited plank");
                    var take=action(bridge,"withdraw");take.addProperty("container_id",((Number)stored.get("id")).intValue());
                    take.addProperty("container_slot",((Number)item.get("slot")).intValue());bridge.act(take);
                    check(count(bridge,Items.OAK_PLANKS)==1,"Withdraw must restore the same item, without duplication");
                    bridge.pause();check(bridge.state().get("container")==null,"Minecraft stop must close storage");
                    bridge.close();server.getPlayerList().remove(owner);phase[0]=4;helper.succeed();
                }
            } catch(Exception error) {phase[0]=4;bridge.close();server.getPlayerList().remove(owner);helper.fail(error.toString());}
        });
    }
}
