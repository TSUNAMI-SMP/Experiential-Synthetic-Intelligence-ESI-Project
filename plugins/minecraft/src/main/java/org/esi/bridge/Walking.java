package org.esi.bridge;

import java.util.*;
import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.phys.Vec3;

/** Local A*: only standable, currently visible foot cells, never teleportation or hidden mining. */
final class Walking {
    static boolean standable(ServerPlayer player, BlockPos p) {
        var level=player.level();
        if (!level.getBlockState(p).getCollisionShape(level,p).isEmpty()
            || !level.getBlockState(p.above()).getCollisionShape(level,p.above()).isEmpty()
            || !level.getFluidState(p).isEmpty() || !level.getFluidState(p.above()).isEmpty()) return false;
        var floor=level.getBlockState(p.below());
        return !floor.getCollisionShape(level,p.below()).isEmpty() && !floor.is(Blocks.MAGMA_BLOCK)
            && !floor.is(Blocks.CAMPFIRE) && !floor.is(Blocks.SOUL_CAMPFIRE)
            && !floor.is(Blocks.CACTUS) && !floor.is(Blocks.SWEET_BERRY_BUSH);
    }
    static Deque<BlockPos> path(ServerPlayer player, Vec3 target, boolean adjacent) {
        BlockPos start=player.blockPosition(), end=BlockPos.containing(target);
        Map<BlockPos,BlockPos> parents=new HashMap<>(); Map<BlockPos,Double> costs=new HashMap<>();
        PriorityQueue<Node> open=new PriorityQueue<>(Comparator.comparingDouble(Node::score));
        Set<BlockPos> closed=new HashSet<>(); costs.put(start,0.0); open.add(new Node(start,0));
        BlockPos found=null;
        while(!open.isEmpty() && closed.size()<512) {
            BlockPos here=open.remove().point();
            if(!closed.add(here)) continue;
            if(here.distManhattan(end)<=(adjacent?1:0) && Math.abs(here.getY()-end.getY())<=1) { found=here; break; }
            for(int[] step:new int[][]{{1,0},{-1,0},{0,1},{0,-1}}) for(int dy:new int[]{0,1,-1}) {
                BlockPos next=here.offset(step[0],dy,step[1]);
                if(next.distManhattan(start)>14 || !standable(player,next)
                    || !Sense.clear(player,new Vec3(next.getX()+.5,next.getY()+1,next.getZ()+.5))) continue;
                if(dy==1 && !player.level().getBlockState(here.above(2)).getCollisionShape(player.level(),here.above(2)).isEmpty()) continue;
                double cost=costs.get(here)+1+Math.abs(dy)*.4;
                if(cost<costs.getOrDefault(next,Double.POSITIVE_INFINITY)) {
                    costs.put(next,cost); parents.put(next,here); open.add(new Node(next,cost+next.distManhattan(end)));
                }
                break;
            }
        }
        Deque<BlockPos> result=new ArrayDeque<>();
        if(found!=null) for(BlockPos cursor=found;!cursor.equals(start);cursor=parents.get(cursor)) result.addFirst(cursor);
        return result;
    }
    private record Node(BlockPos point,double score) {}
}
