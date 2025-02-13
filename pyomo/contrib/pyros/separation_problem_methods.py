"""
Functions for the construction and solving of the GRCS separation problem via ROsolver
"""
from pyomo.core.base.constraint import Constraint, ConstraintList
from pyomo.core.base.objective import Objective, maximize, value
from pyomo.core.base import Var, Param
from pyomo.common.collections import ComponentSet, ComponentMap
from pyomo.common.dependencies import numpy as np
from pyomo.contrib.pyros.util import ObjectiveType, get_time_from_solver, output_logger
from pyomo.contrib.pyros.solve_data import SeparationResult
from pyomo.opt import TerminationCondition as tc
from pyomo.core.expr.current import (
    replace_expressions,
    identify_mutable_parameters,
    identify_variables,
)
from pyomo.contrib.pyros.util import get_main_elapsed_time, is_certain_parameter
from pyomo.contrib.pyros.uncertainty_sets import Geometry
from pyomo.common.errors import ApplicationError
from pyomo.contrib.pyros.util import ABS_CON_CHECK_FEAS_TOL
from pyomo.common.timing import TicTocTimer
from pyomo.contrib.pyros.util import (
    TIC_TOC_SOLVE_TIME_ATTR,
    adjust_solver_time_settings,
    revert_solver_max_time_adjustment,
)
import os
from copy import deepcopy


def add_uncertainty_set_constraints(model, config):
    """
    Add inequality constraint(s) representing the uncertainty set.
    """

    model.util.uncertainty_set_constraint = config.uncertainty_set.set_as_constraint(
        uncertain_params=model.util.uncertain_param_vars, model=model, config=config
    )

    config.uncertainty_set.add_bounds_on_uncertain_parameters(
        model=model, config=config
    )

    # === Pre-process out any uncertain parameters which have q_LB = q_ub via (q_ub - q_lb)/max(1,|q_UB|) <= TOL
    #     before building the uncertainty set constraint(s)
    uncertain_params = config.uncertain_params
    for i in range(len(uncertain_params)):
        if is_certain_parameter(uncertain_param_index=i, config=config):
            # This parameter is effectively certain for this set, can remove it from the uncertainty set
            # We do this by fixing it in separation to its nominal value
            model.util.uncertain_param_vars[i].fix(
                config.nominal_uncertain_param_vals[i]
            )

    return


def make_separation_objective_functions(model, config):
    """
    Inequality constraints referencing control variables, state variables, or uncertain parameters
    must be separated against in separation problem.
    """
    performance_constraints = []
    for c in model.component_data_objects(Constraint, active=True, descend_into=True):
        _vars = ComponentSet(identify_variables(expr=c.expr))
        uncertain_params_in_expr = list(
            v for v in model.util.uncertain_param_vars.values() if v in _vars
        )
        state_vars_in_expr = list(v for v in model.util.state_vars if v in _vars)
        second_stage_variables_in_expr = list(
            v for v in model.util.second_stage_variables if v in _vars
        )
        if not c.equality and (
            uncertain_params_in_expr
            or state_vars_in_expr
            or second_stage_variables_in_expr
        ):
            # This inequality constraint depends on uncertain parameters therefore it must be separated against
            performance_constraints.append(c)
        elif not c.equality and not (
            uncertain_params_in_expr
            or state_vars_in_expr
            or second_stage_variables_in_expr
        ):
            c.deactivate()  # These are x \in X constraints, not active in separation because x is fixed to x* from previous master
    model.util.performance_constraints = performance_constraints
    model.util.separation_objectives = []
    map_obj_to_constr = ComponentMap()

    if len(model.util.performance_constraints) == 0:
        raise ValueError(
            "No performance constraints identified for the postulated robust optimization problem."
        )

    for idx, c in enumerate(performance_constraints):
        # Separation objective constraints standardized to be MAXIMIZATION of <= constraints
        c.deactivate()
        if c.upper is not None:
            # This is an <= constraint, maximized in separation
            obj = Objective(expr=c.body - c.upper, sense=maximize)
            map_obj_to_constr[c] = obj
            model.add_component("separation_obj_" + str(idx), obj)
            model.util.separation_objectives.append(obj)
        elif c.lower is not None:
            # This is an >= constraint, not supported
            raise ValueError(
                "All inequality constraints in model must be in standard form (<= RHS)"
            )

    model.util.map_obj_to_constr = map_obj_to_constr
    for obj in model.util.separation_objectives:
        obj.deactivate()

    return


def make_separation_problem(model_data, config):
    """
    Swap out uncertain param Param objects for Vars
    Add uncertainty set constraints and separation objectives
    """
    separation_model = model_data.original.clone()
    separation_model.del_component("coefficient_matching_constraints")
    separation_model.del_component("coefficient_matching_constraints_index")

    uncertain_params = separation_model.util.uncertain_params
    separation_model.util.uncertain_param_vars = param_vars = Var(
        range(len(uncertain_params))
    )
    map_new_constraint_list_to_original_con = ComponentMap()

    if config.objective_focus is ObjectiveType.worst_case:
        separation_model.util.zeta = Param(initialize=0, mutable=True)
        constr = Constraint(
            expr=separation_model.first_stage_objective
            + separation_model.second_stage_objective
            - separation_model.util.zeta
            <= 0
        )
        separation_model.add_component("epigraph_constr", constr)

    substitution_map = {}
    # Separation problem initialized to nominal uncertain parameter values
    for idx, var in enumerate(list(param_vars.values())):
        param = uncertain_params[idx]
        var.set_value(param.value, skip_validation=True)
        substitution_map[id(param)] = var

    separation_model.util.new_constraints = constraints = ConstraintList()

    uncertain_param_set = ComponentSet(uncertain_params)
    for c in separation_model.component_data_objects(Constraint):
        if any(v in uncertain_param_set for v in identify_mutable_parameters(c.expr)):
            if c.equality:
                constraints.add(
                    replace_expressions(expr=c.lower, substitution_map=substitution_map)
                    == replace_expressions(
                        expr=c.body, substitution_map=substitution_map
                    )
                )
            elif c.lower is not None:
                constraints.add(
                    replace_expressions(expr=c.lower, substitution_map=substitution_map)
                    <= replace_expressions(
                        expr=c.body, substitution_map=substitution_map
                    )
                )
            elif c.upper is not None:
                constraints.add(
                    replace_expressions(expr=c.upper, substitution_map=substitution_map)
                    >= replace_expressions(
                        expr=c.body, substitution_map=substitution_map
                    )
                )
            else:
                raise ValueError(
                    "Unable to parse constraint for building the separation problem."
                )
            c.deactivate()
            map_new_constraint_list_to_original_con[
                constraints[constraints.index_set().last()]
            ] = c

    separation_model.util.map_new_constraint_list_to_original_con = (
        map_new_constraint_list_to_original_con
    )

    # === Add objectives first so that the uncertainty set
    #     Constraints do not get picked up into the set
    # 	  of performance constraints which become objectives
    make_separation_objective_functions(separation_model, config)
    add_uncertainty_set_constraints(separation_model, config)

    # === Deactivate h(x,q) == 0 constraints
    for c in separation_model.util.h_x_q_constraints:
        c.deactivate()

    return separation_model


def get_all_sep_objective_values(model_data, config):
    """
    Returns all violations from separation
    """
    list_of_violations_across_objectives = []
    for o in model_data.separation_model.util.separation_objectives:
        try:
            list_of_violations_across_objectives.append(value(o.expr))
        except:
            for v in model_data.separation_model.util.first_stage_variables:
                config.progress_logger.info(v.name + ' ' + str(v.value))
            for v in model_data.separation_model.util.second_stage_variables:
                config.progress_logger.info(v.name + ' ' + str(v.value))
            raise ArithmeticError(
                "Objective function " + str(o) + " led to a math domain error. "
                "Does this objective (meaning, its parent performance constraint) "
                "contain log(x)  or 1/x functions or others with tricky domains?"
            )
    return list_of_violations_across_objectives


def get_index_of_max_violation(model_data, config, solve_data_list):
    is_discrete_scenarios = (
        True
        if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS
        else False
    )
    matrix_dim = 0
    indices_of_violating_realizations = []
    indices_of_violating_realizations_and_scenario = {}
    if is_discrete_scenarios:
        # There are num_scenarios by num_sep_objectives solutions to consider, take the worst-case per sep_objective
        for idx, row in enumerate(solve_data_list):
            if any(v.found_violation for v in row):
                matrix_dim += 1
                if len([v for v in row if v.found_violation]) > 1:
                    max_val, violation_idx = max(
                        (val.list_of_scaled_violations[idx], the_index)
                        for the_index, val in enumerate(row)
                    )
                else:
                    for elem in row:
                        if elem.found_violation:
                            violation_idx = row.index(elem)
                indices_of_violating_realizations.append(idx)
                indices_of_violating_realizations_and_scenario[idx] = violation_idx
    else:
        matrix_dim = len(
            list(
                result
                for solve_list in solve_data_list
                for result in solve_list
                if result.found_violation == True
            )
        )
        idx_j = 0
        indices_of_violating_realizations.extend(
            i for i, x in enumerate(solve_data_list) if x[idx_j].found_violation == True
        )

    if matrix_dim == 0:
        # no violating realizations
        return None, None

    matrix_of_violations = np.zeros(
        shape=(
            matrix_dim,
            len(model_data.separation_model.util.performance_constraints),
        )
    )
    violation_dict = {}
    if is_discrete_scenarios:
        violation_dict = indices_of_violating_realizations_and_scenario
    else:
        for k in indices_of_violating_realizations:
            for l in range(len(solve_data_list[k])):
                if solve_data_list[k][l].found_violation:
                    violation_dict[k] = l
    for i in range(matrix_dim):
        for j in range(len(model_data.separation_model.util.performance_constraints)):
            if is_discrete_scenarios:
                idx_max_violation_from_scenario = violation_dict[
                    indices_of_violating_realizations[i]
                ]
                matrix_of_violations[i][j] = max(
                    solve_data_list[indices_of_violating_realizations[i]][
                        idx_max_violation_from_scenario
                    ].list_of_scaled_violations[j],
                    0,
                )
            else:
                matrix_of_violations[i][j] = max(
                    solve_data_list[indices_of_violating_realizations[i]][
                        0
                    ].list_of_scaled_violations[j],
                    0,
                )

    sums = []
    for i in range(matrix_of_violations.shape[1]):
        sum = 0
        column = matrix_of_violations[:, i]
        for j in range(len(column)):
            sum += column[j]
        sums.append(sum)
    max_value = max(sums)
    idx_i = sums.index(max_value)

    if is_discrete_scenarios:
        idx_j = violation_dict[idx_i]

    return idx_i, idx_j


def solve_separation_problem(model_data, config):
    # Timing variables
    global_solve_time = 0
    local_solve_time = 0

    # List of objective functions
    objectives_map = model_data.separation_model.util.map_obj_to_constr
    constraint_map_to_master = (
        model_data.separation_model.util.map_new_constraint_list_to_original_con
    )

    # Add additional or remaining separation objectives to the dict
    # (those either not assigned an explicit priority or those added by Pyros for ssv bounds)
    config_sep_priority_dict = config.separation_priority_order
    actual_sep_priority_dict = ComponentMap()
    for perf_con in model_data.separation_model.util.performance_constraints:
        actual_sep_priority_dict[perf_con] = config_sep_priority_dict.get(
            perf_con.name, 0
        )

    # "Bin" the objectives based on priorities
    sorted_unique_priorities = sorted(
        list(set(actual_sep_priority_dict.values())), reverse=True
    )
    set_of_deterministic_constraints = (
        model_data.separation_model.util.deterministic_constraints
    )
    if hasattr(model_data.separation_model, "epigraph_constr"):
        set_of_deterministic_constraints.add(
            model_data.separation_model.epigraph_constr
        )

    # Determine whether to solve separation problems globally as well
    if config.bypass_global_separation:
        separation_cycle = [False]
    elif config.bypass_local_separation:
        separation_cycle = [True]
    else:
        separation_cycle = [False, True]
    for is_global in separation_cycle:
        solver = config.global_solver if is_global else config.local_solver
        solve_data_list = []

        for val in sorted_unique_priorities:
            # Descending ordered by value
            # The list of performance constraints with this priority
            perf_constraints = [
                constr_name
                for constr_name, priority in actual_sep_priority_dict.items()
                if priority == val
            ]
            for perf_con in perf_constraints:
                # config.progress_logger.info("Separating constraint " + str(perf_con))
                try:
                    separation_obj = objectives_map[perf_con]
                except:
                    raise ValueError(
                        "Error in mapping separation objective to its master constraint form."
                    )
                separation_obj.activate()

                if perf_con in set_of_deterministic_constraints:
                    nom_constraint = perf_con
                else:
                    nom_constraint = constraint_map_to_master[perf_con]

                try:
                    model_data.master_nominal_scenario_value = value(
                        model_data.master_nominal_scenario.find_component(
                            nom_constraint
                        )
                    )
                except:
                    raise ValueError(
                        "Unable to access nominal scenario value for the constraint "
                        + str(nom_constraint)
                    )

                if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS:
                    solve_data_list.append(
                        discrete_solve(
                            model_data=model_data,
                            config=config,
                            solver=solver,
                            is_global=is_global,
                        )
                    )
                    if all(
                        s.termination_condition in globally_acceptable
                        for sep_soln_list in solve_data_list
                        for s in sep_soln_list
                    ) or (
                        is_global == False
                        and all(
                            s.termination_condition in locally_acceptable
                            for sep_soln_list in solve_data_list
                            for s in sep_soln_list
                        )
                    ):
                        exit_separation_loop = False
                    else:
                        exit_separation_loop = True
                else:
                    solve_data = SeparationResult()
                    exit_separation_loop = solver_call_separation(
                        model_data=model_data,
                        config=config,
                        solver=solver,
                        solve_data=solve_data,
                        is_global=is_global,
                    )
                    solve_data_list.append([solve_data])

                # === Keep track of total solve times
                if is_global:
                    if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS:
                        for sublist in solve_data_list:
                            for s in sublist:
                                global_solve_time += get_time_from_solver(s.results)
                    else:
                        global_solve_time += get_time_from_solver(solve_data.results)
                else:
                    if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS:
                        for sublist in solve_data_list:
                            for s in sublist:
                                local_solve_time += get_time_from_solver(s.results)
                    else:
                        local_solve_time += get_time_from_solver(solve_data.results)

                # === Terminate for timing
                if exit_separation_loop:
                    return (
                        solve_data_list,
                        [],
                        [],
                        is_global,
                        local_solve_time,
                        global_solve_time,
                    )
                separation_obj.deactivate()

        # Do we return?
        # If there are multiple violations in this bucket, pick the worst-case
        idx_i, idx_j = get_index_of_max_violation(
            model_data=model_data, config=config, solve_data_list=solve_data_list
        )

        if (idx_i, idx_j) != (None, None):
            violating_realizations = [
                v for v in solve_data_list[idx_i][idx_j].violating_param_realization
            ]
            violations = solve_data_list[idx_i][idx_j].list_of_scaled_violations
        else:
            violating_realizations = []
            violations = []

        if any(s.found_violation for solve_list in solve_data_list for s in solve_list):
            # config.progress_logger.info(
            # 	"Violation found in constraint %s with realization %s" % (
            # 	list(objectives_map.keys())[idx_i], violating_realizations))
            return (
                solve_data_list,
                violating_realizations,
                violations,
                is_global,
                local_solve_time,
                global_solve_time,
            )

    return solve_data_list, [], [], is_global, local_solve_time, global_solve_time


def get_absolute_tol(model_data, config):
    nom_value = model_data.master_nominal_scenario_value
    denom = float(max(1, abs(nom_value)))
    tol = config.robust_feasibility_tolerance
    return denom * tol, nom_value


def update_solve_data_violations(model_data, config, solve_data):
    """
    Evaluate the inequality constraint function violations
    of the current separation model solution, and store the
    results in a given `SeparationResult` object.
    Also, determine whether the separation solution violates
    the inequality constraint whose body is the model's
    active objective.

    Parameters
    ----------
    model_data : SeparationProblemData
        Object containing the separation model.
    config : ConfigDict
        PyROS solver settings.
    solve_data : SeparationResult
        Result for most recent separation problem.

    Returns
    -------
    : bool
        True if constraint is violated, False otherwise.
    """

    nom_value = model_data.master_nominal_scenario_value
    denom = float(max(1, abs(nom_value)))
    tol = config.robust_feasibility_tolerance
    active_objective = next(
        model_data.separation_model.component_data_objects(Objective, active=True)
    )

    # update solve data attributes
    solve_data.violating_param_realization = list(
        p.value for p in model_data.separation_model.util.uncertain_param_vars.values()
    )
    list_of_violations = get_all_sep_objective_values(
        model_data=model_data, config=config
    )
    solve_data.list_of_scaled_violations = [l / denom for l in list_of_violations]

    return value(active_objective) / denom > tol


def initialize_separation(model_data, config):
    """
    Initialize separation problem variables, and fix all first-stage
    variables to their corresponding values from most recent
    master problem solution.

    Parameters
    ----------
    model_data : SeparationProblemData
        Separation problem data.
    config : ConfigDict
        PyROS solver settings.

    Note
    ----
    If a static DR policy is used, then all second-stage variables
    are fixed and the decision rule equations are deactivated.

    The point to which the separation model is initialized should,
    in general, be feasible, provided the set does not have a
    discrete geometry (as there is no master model block corresponding
    to any of the remaining discrete scenarios against which we
    separate).
    """
    # initialize to values from nominal block if nominal objective.
    # else, initialize to values from latest block added to master
    if config.objective_focus == ObjectiveType.nominal:
        block_num = 0
    else:
        block_num = model_data.iteration

    master_blk = model_data.master_model.scenarios[block_num, 0]
    master_blks = list(model_data.master_model.scenarios.values())
    fsv_set = ComponentSet(master_blk.util.first_stage_variables)
    sep_model = model_data.separation_model

    def get_parent_master_blk(var):
        """
        Determine the master model scenario block of which
        a given variable is a child component (or descendant).
        """
        parent = var.parent_block()
        while parent not in master_blks:
            parent = parent.parent_block()
        return parent

    for master_var in master_blk.component_data_objects(Var, active=True):
        # parent block of the variable need not be `master_blk`
        # (e.g. for first stage and decision rule variables, it
        # may be the nominal block)
        parent_master_blk = get_parent_master_blk(master_var)
        sep_var_name = master_var.getname(
            relative_to=parent_master_blk, fully_qualified=True
        )

        # initialize separation problem var to value from master block
        sep_var = sep_model.find_component(sep_var_name)
        sep_var.set_value(value(master_var, exception=False))

        # fix first-stage variables (including decision rule vars)
        if master_var in fsv_set:
            sep_var.fix()

    # initialize uncertain parameter variables to most recent
    # point added to master
    if config.uncertainty_set.geometry != Geometry.DISCRETE_SCENARIOS:
        param_vars = sep_model.util.uncertain_param_vars
        latest_param_values = model_data.points_added_to_master[block_num]
        for param_var, val in zip(param_vars.values(), latest_param_values):
            param_var.set_value(val)

    # if static approximation, fix second-stage variables
    # and deactivate the decision rule equations
    for c in model_data.separation_model.util.second_stage_variables:
        if config.decision_rule_order != 0:
            c.unfix()
        else:
            c.fix()
    if config.decision_rule_order == 0:
        for v in model_data.separation_model.util.decision_rule_eqns:
            v.deactivate()
        for v in model_data.separation_model.util.decision_rule_vars:
            v.fix()

    if any(c.active for c in model_data.separation_model.util.h_x_q_constraints):
        raise AttributeError(
            "All h(x,q) type constraints must be deactivated in separation."
        )

    # check: initial point feasible?
    for con in sep_model.component_data_objects(Constraint, active=True):
        lb, val, ub = value(con.lb), value(con.body), value(con.ub)
        lb_viol = val < lb - ABS_CON_CHECK_FEAS_TOL if lb is not None else False
        ub_viol = val > ub + ABS_CON_CHECK_FEAS_TOL if ub is not None else False
        if lb_viol or ub_viol:
            config.progress_logger.debug(con.name, lb, val, ub)


locally_acceptable = {tc.optimal, tc.locallyOptimal, tc.globallyOptimal}
globally_acceptable = {tc.optimal, tc.globallyOptimal}


def solver_call_separation(model_data, config, solver, solve_data, is_global):
    """
    Invoke subordinate solver(s) on separation problem.

    Parameters
    ----------
    model_data : SeparationProblemData
        Separation problem data.
    config : ConfigDict
        PyROS solver settings.
    solver : solver type
        Primary subordinate optimizer with which to solve
        the model.
    solve_data : SeparationResult
        Container for separation problem result.
    is_global : bool
        Is separation problem to be solved globally.

    Returns
    -------
    : bool
        True if separation problem was not solved to an appropriate
        optimality status by any of the solvers available or the
        PyROS elapsed time limit is exceeded, False otherwise.
    """
    if is_global:
        backup_solvers = deepcopy(config.backup_global_solvers)
    else:
        backup_solvers = deepcopy(config.backup_local_solvers)
    backup_solvers.insert(0, solver)

    solver_status_dict = {}
    nlp_model = model_data.separation_model

    # === Initialize separation problem; fix first-stage variables
    initialize_separation(model_data, config)

    timer = TicTocTimer()
    for opt in backup_solvers:
        orig_setting, custom_setting_present = adjust_solver_time_settings(
            model_data.timing, opt, config
        )
        timer.tic(msg=None)
        try:
            results = opt.solve(
                nlp_model,
                tee=config.tee,
                load_solutions=False,
                symbolic_solver_labels=True,
            )
        except ApplicationError:
            # account for possible external subsolver errors
            # (such as segmentation faults, function evaluation
            # errors, etc.)
            config.progress_logger.error(
                f"Solver {repr(opt)} encountered exception attempting to "
                f"optimize master problem in iteration {model_data.iteration}"
            )
            raise
        else:
            setattr(results.solver, TIC_TOC_SOLVE_TIME_ATTR, timer.toc(msg=None))
        finally:
            revert_solver_max_time_adjustment(
                opt, orig_setting, custom_setting_present, config
            )

        # record termination condition for this particular solver
        solver_status_dict[str(opt)] = results.solver.termination_condition
        solve_data.termination_condition = results.solver.termination_condition
        solve_data.results = results

        # has PyROS time limit been reached?
        elapsed = get_main_elapsed_time(model_data.timing)
        if config.time_limit:
            if elapsed >= config.time_limit:
                solve_data.found_violation = False
                return True

        # if separation problem solved to optimality, record results
        # and exit
        acceptable_conditions = globally_acceptable if is_global else locally_acceptable
        optimal_termination = solve_data.termination_condition in acceptable_conditions
        if optimal_termination:
            nlp_model.solutions.load_from(results)
            solve_data.found_violation = update_solve_data_violations(
                model_data, config, solve_data
            )
            return False

    # problem not solved successfully, so no violation found
    solve_data.found_violation = False

    # All subordinate solvers failed to optimize model to appropriate
    # termination condition. PyROS will terminate with subsolver
    # error. At this point, export model if desired
    save_dir = config.subproblem_file_directory
    if save_dir and config.keepfiles:
        objective = str(
            list(nlp_model.component_data_objects(Objective, active=True))[0].name
        )
        name = os.path.join(
            save_dir,
            (
                config.uncertainty_set.type
                + "_"
                + nlp_model.name
                + "_separation_"
                + str(model_data.iteration)
                + "_obj_"
                + objective
                + ".bar"
            ),
        )
        nlp_model.write(name, io_options={'symbolic_solver_labels': True})
        output_logger(
            config=config,
            separation_error=True,
            filename=name,
            iteration=model_data.iteration,
            objective=objective,
            status_dict=solver_status_dict,
        )
    return True


def discrete_solve(model_data, config, solver, is_global):
    """
    Loops over discrete scenarios, solving square problem to determine constraint violation in separation objective.
    """
    # Constraint are grouped by dim(uncertain_param) groups for each scenario in D
    solve_data_list = []
    # === Remove (skip over) already accounted for violations
    chunk_size = len(model_data.separation_model.util.uncertain_param_vars)
    conlist = model_data.separation_model.util.uncertainty_set_constraint
    _constraints = list(conlist.values())
    constraints_to_skip = ComponentSet()
    conlist.deactivate()

    for pnt in model_data.points_added_to_master:
        _idx = config.uncertainty_set.scenarios.index(tuple(pnt))
        skip_index_list = list(range(chunk_size * _idx, chunk_size * _idx + chunk_size))
        for _index in range(len(_constraints)):
            if _index in skip_index_list:
                constraints_to_skip.add(_constraints[_index])
    constraints = list(c for c in _constraints if c not in constraints_to_skip)

    for i in range(0, len(constraints), chunk_size):
        chunk = list(constraints[i : i + chunk_size])
        for idx, con in enumerate(chunk):
            con.activate()
            model_data.separation_model.util.uncertain_param_vars[idx].fix(con.lower)
            con.deactivate()
        solve_data = SeparationResult()
        solver_call_separation(
            model_data=model_data,
            config=config,
            solver=solver,
            solve_data=solve_data,
            is_global=is_global,
        )
        solve_data_list.append(solve_data)
        for con in chunk:
            con.deactivate()

    return solve_data_list
