package org.esi.bridge;

import com.google.gson.JsonObject;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.world.level.block.Blocks;

public class LifeBridgeGameTest {
    @GameTest(maxTicks=300,skyAccess=true)
    public void shallowHoleExitAndOwnerConversation(GameTestHelper helper) throws Exception {
        var server=helper.getLevel().getServer();
        for(int x=0;x<8;x++)for(int z=0;z<8;z++) {
            helper.setBlock(x,0,z,Blocks.STONE);
            helper.setBlock(x,1,z,x>=2 && x<=4 && z>=2 && z<=4 ? Blocks.AIR : Blocks.STONE);
        }
        var owner=helper.makeMockServerPlayerInLevel();server.getPlayerList().setViewDistance(2);
        var start=helper.absolutePos(new BlockPos(3,1,3));
        owner.teleportTo(helper.getLevel(),start.getX()+.5,start.getY(),start.getZ()+.5,Set.of(),0,0,true);
        Bridge bridge=new Bridge(server);bridge.authorize(owner);
        JsonObject connect=new JsonObject();connect.addProperty("protocol",1);connect.addProperty("plugin_id","minecraft.fabric");
        connect.addProperty("character_id",UUID.randomUUID().toString());
        var connection=bridge.connect(connect);String lease=connection.get("lease").toString();
        if(!bridge.hear(owner,"何が気になっているの？"))throw new IllegalStateException("Owner conversation was rejected");
        if(bridge.hear(bridge.bot,"これは自己出力"))throw new IllegalStateException("Self output must not become a user message");
        var state=bridge.state();
        if(((List<?>)state.get("messages")).size()!=1)throw new IllegalStateException("Missing addressed owner message");
        JsonObject reply=new JsonObject();reply.addProperty("id","test-reply");reply.addProperty("text","周りの地形を調べたい。");
        bridge.say(reply);bridge.say(reply); // Replayed delivery is idempotent.
        if(!Set.of("day","night").contains(state.get("daylight")))throw new IllegalStateException("Missing sky observation");
        var targets=(List<Map<String,Object>>)state.get("targets");
        var waypoint=targets.stream().filter(t -> t.get("kind").equals("waypoint") &&
            ((Number)t.get("y")).doubleValue()>start.getY()+.5 &&
            !Walking.path(bridge.bot,new net.minecraft.world.phys.Vec3(((Number)t.get("x")).doubleValue(),
                ((Number)t.get("y")).doubleValue(),((Number)t.get("z")).doubleValue()),false).isEmpty()).findFirst().orElseThrow();
        JsonObject action=new JsonObject();action.addProperty("request_id",UUID.randomUUID().toString().replace("-",""));
        action.addProperty("observation_id",state.get("id").toString());action.addProperty("kind","go_to");
        action.addProperty("target_id",waypoint.get("id").toString());action.addProperty("ticks",160);
        bridge.act(action);
        boolean[] finished={false};
        helper.onEachTick(() -> {
            if(finished[0])return;
            try {
                bridge.checkLease(lease);bridge.tick();
                var current=bridge.state();var result=(Map<?,?>)current.get("action");
                if("failed".equals(result.get("stage")))throw new IllegalStateException("Cannot leave shallow hole: "+result);
                if("completed".equals(result.get("stage"))) {
                    if(bridge.bot.getY()<start.getY()+.8)throw new IllegalStateException("Body did not climb out");
                    finished[0]=true;bridge.close();server.getPlayerList().remove(owner);helper.succeed();
                }
            }catch(Exception error){finished[0]=true;bridge.close();helper.fail(error.toString());}
        });
    }
}
