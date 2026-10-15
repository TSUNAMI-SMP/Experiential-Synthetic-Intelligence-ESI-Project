package org.esi.bridge;

import com.google.gson.JsonObject;
import java.util.*;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.phys.Vec3;

public class BridgeGameTest {
    private static void check(boolean condition,String message) {
        if(!condition) throw new IllegalStateException(message);
    }
    private static JsonObject request(Bridge bridge,String kind,String target,int slot,int ticks) {
        Map<String,Object> state=bridge.state();JsonObject action=new JsonObject();
        action.addProperty("request_id",UUID.randomUUID().toString().replace("-",""));
        action.addProperty("observation_id",state.get("id").toString());action.addProperty("kind",kind);
        action.addProperty("target_id",target);action.addProperty("slot",slot);action.addProperty("ticks",ticks);
        return action;
    }
    private static int count(ServerPlayer player,net.minecraft.world.item.Item item) {
        int result=0;for(int slot=0;slot<36;slot++)if(player.getInventory().getItem(slot).is(item))result+=player.getInventory().getItem(slot).getCount();
        return result;
    }
    @GameTest(maxTicks=600,skyAccess=true)
    public void realSurvivalBodyMiningEatingAndPersistence(GameTestHelper helper) throws Exception {
        var level=helper.getLevel();var server=level.getServer();
        for(int x=0;x<8;x++)for(int z=0;z<8;z++)helper.setBlock(x,0,z,Blocks.STONE);
        helper.setBlock(3,1,5,Blocks.STONE);helper.setBlock(3,-1,5,Blocks.DIAMOND_ORE);
        ServerPlayer owner=helper.makeMockServerPlayerInLevel();
        server.getPlayerList().setViewDistance(2);
        BlockPos start=helper.absolutePos(new BlockPos(3,1,3));
        owner.teleportTo(level,start.getX()+.5,start.getY(),start.getZ()+.5,Set.of(),0,0,true);
        Bridge bridge=new Bridge(server);bridge.authorize(owner);
        JsonObject connect=new JsonObject();connect.addProperty("protocol",1);connect.addProperty("plugin_id","minecraft.fabric");
        connect.addProperty("character_id",UUID.randomUUID().toString());connect.addProperty("name","ESI Gameplay Test");
        Map<String,Object> connection=bridge.connect(connect);String lease=connection.get("lease").toString();
        check(bridge.bot!=null && bridge.bot!=owner,"ESI must be a separate player");
        check(!bridge.bot.isCreative() && !bridge.bot.getAbilities().flying,"ESI must use survival physics");
        check(count(bridge.bot,Items.BREAD)==0,"Production connection must not give food");
        // Fixtures exist only in this separate test mod, never in the production bridge.
        bridge.bot.getInventory().setItem(0,new ItemStack(Items.WOODEN_PICKAXE));
        bridge.bot.getInventory().setItem(10,new ItemStack(Items.BREAD,2));
        bridge.bot.getFoodData().setFoodLevel(8);bridge.bot.getFoodData().setSaturation(0);
        BlockPos stone=helper.absolutePos(new BlockPos(3,1,5));
        String target="b:"+stone.getX()+","+stone.getY()+","+stone.getZ();
        Map<String,Object> initial=bridge.state();
        check(Bridge.JSON.toJson(initial.get("targets")).contains(target),"Missing visible stone: "+Bridge.JSON.toJson(initial));
        check(!Bridge.JSON.toJson(initial.get("targets")).contains("minecraft:diamond_ore"),"Hidden ore must not be exposed");
        JsonObject invalid=request(bridge,"mine","b:999999,64,999999",0,20);
        try {bridge.act(invalid);throw new IllegalStateException("Unobserved target accepted");}
        catch(IllegalArgumentException expected) {}
        bridge.act(request(bridge,"mine",target,0,160));
        int[] phase={0}, ticks={0};Vec3[] position={bridge.bot.position()};
        helper.onEachTick(() -> {
            if(phase[0]>=6)return;
            try {
                ticks[0]++;if(phase[0]<4)bridge.checkLease(lease);bridge.tick();
                if(phase[0]==0 && ticks[0]>180) throw new IllegalStateException("Mining stalled: "+Bridge.JSON.toJson(bridge.state()));
                if(phase[0]==0 && level.getBlockState(stone).isAir()) {
                    check(ticks[0]>1,"Mining must take real game ticks");
                    check(bridge.bot.getInventory().getItem(0).getDamageValue()>0,"Vanilla mining must wear the tool");
                    phase[0]=1;ticks[0]=0;
                    JsonObject move=request(bridge,"move","",0,20);move.addProperty("forward",1);bridge.act(move);
                } else if(phase[0]==1 && ticks[0]>=30) {
                    check(bridge.bot.position().distanceTo(position[0])>.3,"Fake player must physically move");
                    check(count(bridge.bot,Items.COBBLESTONE)>=1,"Vanilla item pickup must add the mined drop");
                    phase[0]=2;ticks[0]=0;bridge.act(request(bridge,"eat","",10,80));
                } else if(phase[0]==2 && ticks[0]==10) {
                    check(bridge.bot.getFoodData().getFoodLevel()==8,"Eating must not refill food instantly");
                } else if(phase[0]==2 && ticks[0]>=55) {
                    check(bridge.bot.getFoodData().getFoodLevel()>8,"Vanilla food gauge must increase");
                    check(count(bridge.bot,Items.BREAD)==1,"A food item must actually be consumed");
                    phase[0]=3;ticks[0]=0;
                    JsonObject move=request(bridge,"move","",0,100);move.addProperty("forward",-1);bridge.act(move);
                } else if(phase[0]==3 && ticks[0]==4) {bridge.stop("test stop");position[0]=bridge.bot.position();}
                else if(phase[0]==3 && ticks[0]>=14) {
                    check(bridge.bot.position().distanceTo(position[0])<.5,"Stop must release movement");
                    position[0]=bridge.bot.position();
                    bridge.bot.setHealth(17);bridge.disconnect();phase[0]=4;ticks[0]=0;
                } else if(phase[0]==4 && ticks[0]>=4) {
                    Map<String,Object> next=bridge.connect(connect);
                    // A new connection owns a new lease; no old command may survive it.
                    check(!next.get("lease").equals(lease),"Reconnect must rotate its lease");
                    check(count(bridge.bot,Items.BREAD)==1,"Minecraft inventory must survive reconnect");
                    check(bridge.bot.getHealth()==17,"Reconnect must not heal the player");
                    check(bridge.bot.position().distanceTo(position[0])<.3,"Reconnect must preserve position");
                    bridge.pause();
                    try {bridge.act(request(bridge,"move","",0,20));throw new IllegalStateException("Paused input accepted");}
                    catch(IllegalStateException expected) {check(expected.getMessage().contains("停止"),"Pause must reject input");}
                    bridge.connect(connect);
                    try {bridge.checkLease(lease);throw new IllegalStateException("Old lease accepted");}
                    catch(SecurityException expected) {}
                    var targets=(List<Map<String,Object>>)bridge.state().get("targets");
                    var waypoint=targets.stream().filter(t -> t.get("kind").equals("waypoint")
                        && ((Number)t.get("distance")).doubleValue()>2.5).findFirst().orElseThrow();
                    bridge.act(request(bridge,"go_to",waypoint.get("id").toString(),0,160));
                    phase[0]=5;ticks[0]=0;
                } else if(phase[0]==5 && bridge.state().get("action") instanceof Map<?,?> action
                    && action.get("stage").equals("completed")) {
                    check(bridge.bot.position().distanceTo(position[0])>.5,"A local path must move the body");
                    server.getPlayerList().remove(owner);bridge.tick();
                    check(bridge.bot==null,"Owner exit must disconnect ESI");
                    bridge.close();phase[0]=6;helper.succeed();
                }
            } catch(Exception exception) {
                bridge.close();phase[0]=6;helper.fail(exception.toString());
            }
        });
    }
}
