import os
import math

import numpy as np

import torch

import warp as wp




@wp.kernel
def count_neighbors(grid : wp.uint64,
                    radius: wp.types.float32,
                    points_to_check: wp.array(dtype=wp.vec3),
                    refArray: wp.array(dtype=wp.vec3),
                    counts: wp.array(dtype=wp.types.float32),
                    ):
    tid = wp.tid()
    #print(tid)
    # order threads by cell
    #i = wp.hash_grid_point_id(grid, tid)
    #print(i)
    # # current point    
    p = points_to_check[tid]

    minn = float(1000000)
    # construct query around point p
    neighbors = wp.hash_grid_query(grid, p, radius)
    for index in neighbors:
        # compute distance to point
        d = wp.length(p - refArray[index])
        #if (d <= radius):
        minn= wp.min(d,minn)
        #print(d)
    counts[tid] = minn
    

def prepare_tensors_for_warp_loss(y_true, y_hat,radius,device):
    """
    y_true and y_hatshould be boolean tensors with the same dimensions
    we need to parse the arrays to format of indicies in order to be able to use warp with HashGrid
    """
    # y_true=y_true[0,0,:,:,:]
    # y_hat=y_hat[0,0,:,:,:]

    sizz= y_true.size()
    dim_x = sizz[0]
    dim_y = sizz[1]
    dim_z = sizz[2]


    #true positives
    eqq =torch.logical_and(y_true,y_hat)  #torch.eq(labelBoolTensorA,summB)
    #false negatives labelBoolTensorA - gold stadard
    fn=  torch.logical_and(torch.logical_not(eqq),y_true)
    #false positives labelBoolTensorA - gold stadard
    fp=  torch.logical_and(torch.logical_not(eqq),y_hat)
    ####first we will look around of all fp points     
    num_points_fp = torch.sum(fp).item()
    #to sparse - to get indicies - that transpose for correct dim
    fpIndicies = torch.from_numpy(np.argwhere(fp.cpu().numpy())).type(torch.float32).contiguous().to('cuda') 
    goldIndicies =  torch.from_numpy(np.argwhere(y_true.cpu().numpy())).type(torch.float32).contiguous().to('cuda') 
    counts_arr_fp = torch.zeros(num_points_fp, dtype=torch.float32).to('cuda')     
 
    num_points_fn = torch.sum(fn).item()
    fnIndicies = torch.from_numpy(np.argwhere(fn.cpu().numpy())).type(torch.float32).contiguous().to('cuda') 
    segmIndicies =  torch.from_numpy(np.argwhere(y_hat.cpu().numpy())).type(torch.float32).contiguous().to('cuda') 
    counts_arr_fn = torch.zeros(num_points_fn, dtype=torch.float32).to('cuda') 


    # fpIndicies = torch.t(fp.to_sparse().indices()).type(torch.float32).contiguous().to('cuda') 
    # goldIndicies =  torch.t(y_true.to_sparse().indices()).type(torch.float32).contiguous().to('cuda') 
    # counts_arr_fp = torch.zeros(num_points_fp, dtype=torch.float32).to('cuda')     
 
    # num_points_fn = torch.sum(fn).item()
    # fnIndicies = torch.t(fn.to_sparse().indices()).type(torch.float32).contiguous().to('cuda') 
    # segmIndicies =  torch.t(y_hat.to_sparse().indices()).type(torch.float32).contiguous().to('cuda') 
    # counts_arr_fn = torch.zeros(num_points_fn, dtype=torch.float32).to('cuda') 

    return (counts_arr_fp,counts_arr_fn,fpIndicies,goldIndicies,fnIndicies,segmIndicies
    ,radius,device,dim_x,dim_y,dim_z,num_points_fp, num_points_fn
    )
    ####secondly we will look around of all fn points 




class getHausdorff(torch.autograd.Function):
    """
    based on example from https://github.com/NVIDIA/warp/blob/main/examples/example_sim_fk_grad_torch.py
    """



    @staticmethod
    def forward(ctx
    ,counts_arr_fp,counts_arr_fn,fpIndicies,goldIndicies,fnIndicies,segmIndicies
    ,radius,device,dim_x,dim_y,dim_z,num_points_fp, num_points_fn
    ):
        ctx.tape = wp.Tape()
        ctx.fpIndicies=wp.from_torch( fpIndicies, dtype=wp.vec3)
        ctx.goldIndicies=wp.from_torch( goldIndicies, dtype=wp.vec3)
        ctx.counts_arr_fp=wp.from_torch(counts_arr_fp , dtype=wp.types.float32)
        ctx.fnIndicies=wp.from_torch(fnIndicies , dtype=wp.vec3)
        ctx.segmIndicies=wp.from_torch( segmIndicies, dtype=wp.vec3)
        ctx.counts_arr_fn=wp.from_torch( counts_arr_fn, dtype=float)
        wp.synchronize()

        grid = wp.HashGrid(dim_x, dim_y, dim_z, device)
        grid.build(ctx.goldIndicies, radius)
        gridB = wp.HashGrid(dim_x, dim_y, dim_z, device)
        gridB.build(ctx.segmIndicies, radius)


        wp.synchronize()


        with ctx.tape:
            wp.launch(kernel=count_neighbors, dim=  num_points_fp, inputs=[grid.id
                    ,radius,
                    ctx.fpIndicies,
                    ctx.goldIndicies,
                    ctx.counts_arr_fp
                    ], device=device)

            wp.launch(kernel=count_neighbors, dim=  num_points_fn, inputs=[gridB.id
                                ,radius,
                                ctx.fnIndicies,
                                ctx.segmIndicies,
                                ctx.counts_arr_fn
                                ], device=device)



        return (wp.to_torch(ctx.counts_arr_fp),
                wp.to_torch(ctx.counts_arr_fn))



    @staticmethod
    def backward(ctx,fpIndicies,goldIndicies,counts_arr_fp,fnIndicies,segmIndicies, counts_arr_fn,radius
    ,device,dim_x,dim_y,dim_z,num_points_fp, num_points_fn):

        # map incoming Torch grads to our output variables
        ctx.fpIndicies=wp.from_torch( fpIndicies, dtype=wp.vec3)
        ctx.goldIndicies=wp.from_torch( goldIndicies, dtype=wp.vec3)
        ctx.counts_arr_fp=wp.from_torch(counts_arr_fp , dtype=wp.types.float32)
        ctx.fnIndicies=wp.from_torch(fnIndicies , dtype=wp.vec3)
        ctx.segmIndicies=wp.from_torch( segmIndicies, dtype=wp.vec3)
        ctx.counts_arr_fn=wp.from_torch( counts_arr_fn, dtype=wp.types.float32)

        ctx.tape.backward()

        # return adjoint w.r.t. inputs
        return (wp.to_torch(ctx.tape.gradients[ctx.fpIndicies]), 
                wp.to_torch(ctx.tape.gradients[ctx.goldIndicies]),
                wp.to_torch(ctx.tape.gradients[ctx.counts_arr_fp]),
                wp.to_torch(ctx.tape.gradients[ctx.fnIndicies]),
                wp.to_torch(ctx.tape.gradients[ctx.segmIndicies]),
                wp.to_torch(ctx.tape.gradients[ctx.counts_arr_fn])
                 ,None,None,None,None,None,None, None, None)